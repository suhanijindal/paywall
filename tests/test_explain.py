from __future__ import annotations
"""
Tests for the /explain endpoint (app/main.py).

The track's bar is that every money action must be EXPLAINABLE. These tests
check that the plain-English narrative built from the events table actually
names the things a human needs to audit a purchase - the product, the price we
looked up, and the spending limit - and that a refused attempt is explained
just as clearly as a successful one.

Each test gets a fresh SQLite database via tmp_path, so no test can see the
orders or events written by another.
"""

import asyncio

import pytest
from starlette.testclient import TestClient

from app import catalog, main, store


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Point the store at a throwaway database per test, then create tables."""
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    store.init_db()
    yield


@pytest.fixture
def client():
    # Constructed WITHOUT a context manager on purpose: we do not want the
    # lifespan's background reconciliation loop running during a unit test. The
    # fresh_db fixture already created the tables the endpoint needs.
    return TestClient(main.app)


def make_attempt(sku, quantity, limit_paise, key):
    """Drive a purchase through the same shared core the API uses."""
    product = catalog.BY_SKU[sku]
    return asyncio.run(main.create_purchase(product, quantity, limit_paise, key))


def test_successful_order_narrative_names_product_price_and_limit(client):
    # Prevents: a narrative that omits the facts a human needs to audit the
    # money - what was bought, the price WE looked up, and the limit it was
    # checked against. Masala chai at Rs 549 under a Rs 900 limit.
    result = make_attempt("TEA-003", 1, 90000, "explain-ok-1")
    assert result["outcome"] == "created"

    resp = client.get("/explain/{}".format(result["order_id"]))
    assert resp.status_code == 200
    narrative = resp.json()["narrative"]

    assert "Masala Chai" in narrative     # the product
    assert "Rs 549" in narrative          # the price the merchant looked up
    assert "Rs 900" in narrative          # the spending limit
    assert "created" in narrative.lower()
    assert "did not supply this price" in narrative


def test_refused_order_narrative_gives_the_reason(client):
    # Prevents: a refusal that is invisible or unexplained. The cast iron kettle
    # is Rs 2499; a Rs 500 limit must refuse it, and /explain must say why.
    result = make_attempt("ACC-001", 1, 50000, "explain-refuse-1")
    assert result["outcome"] == "over_limit"

    resp = client.get("/explain/{}".format(result["order_id"]))
    assert resp.status_code == 200
    narrative = resp.json()["narrative"]

    assert "Cast Iron Kettle" in narrative
    assert "Rs 2,499" in narrative
    assert "Rs 500" in narrative
    assert "exceeds" in narrative.lower()
    assert "no payment was attempted" in narrative.lower()


def test_refused_http_request_returns_order_id_and_is_explainable(client):
    # Prevents THE gap this change is about: a refused API request that carries
    # no order_id, leaving nothing to call /explain on. A refusal is a lost sale
    # a merchant will want justified, so it must be the most explainable case.
    # Cast iron kettle is Rs 2499; a Rs 500 limit must refuse it.
    resp = client.post("/agent/buy", json={
        "query": "cast iron kettle",
        "max_price_paise": 50000,
        "quantity": 1,
        "idempotency_key": "explain-http-refuse-1",
    })
    assert resp.status_code == 400

    detail = resp.json()["detail"]
    order_id = detail["order_id"]        # the refusal carries an order_id...
    assert order_id.startswith("ord_")

    # ...but it must NOT have become a row in the orders table. It was never
    # an order - only an events-table record of a refused attempt.
    assert store.get_order(order_id) is None

    exp = client.get("/explain/{}".format(order_id))
    assert exp.status_code == 200
    narrative = exp.json()["narrative"]

    assert "Cast Iron Kettle" in narrative          # the product
    assert "Rs 2,499" in narrative                  # its price
    assert "Rs 500" in narrative                    # the limit
    assert "no payment was attempted" in narrative.lower()


def test_unknown_order_id_returns_404(client):
    # Prevents: inventing a story for an order that does not exist.
    resp = client.get("/explain/ord_does_not_exist")
    assert resp.status_code == 404
