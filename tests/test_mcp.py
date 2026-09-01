from __future__ import annotations
"""
Tests for the MCP server (app/mcp_server.py).

These drive the server the same way an AI assistant would: by calling its
JSON-RPC dispatch with initialize / tools/list / tools/call messages. Each test
says, in a comment, which real-world failure it is there to prevent.

Every test gets a fresh SQLite database via tmp_path, so no test can see the
orders or events written by another.
"""

import pytest

from app import mcp_server, store


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Point the store at a throwaway database per test, then create tables."""
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    store.init_db()
    yield


def call(method, params=None, rpc_id=1):
    request = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
    if params is not None:
        request["params"] = params
    return mcp_server.dispatch(request)


def tool_call(name, arguments):
    return call("tools/call", {"name": name, "arguments": arguments})


def text_of(response):
    return response["result"]["content"][0]["text"]


def event_kinds():
    return [e["kind"] for e in store.list_events()]


def test_initialize_reports_the_right_protocol():
    # Prevents: an assistant refusing to connect because we answered the
    # handshake with the wrong protocol version or a malformed result.
    resp = call("initialize", {})
    result = resp["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert result["capabilities"] == {"tools": {}}
    assert result["serverInfo"]["name"] == "paywall"


def test_tools_list_has_exactly_three_tools():
    # Prevents: shipping a tool we did not mean to expose, or dropping one, so
    # the assistant sees an inventory that does not match the docs.
    resp = call("tools/list")
    tools = resp["result"]["tools"]
    assert len(tools) == 3
    assert {t["name"] for t in tools} == {"search_products", "get_product", "purchase"}


def test_search_for_chai_finds_the_masala_chai():
    # Prevents: a broken search that would leave an assistant unable to find a
    # product it was explicitly asked for.
    resp = tool_call("search_products", {"query": "chai"})
    text = text_of(resp)
    assert "TEA-003" in text
    assert "Masala Chai" in text


def test_purchase_over_the_limit_is_refused():
    # Prevents: the core failure this whole project exists to stop - a purchase
    # going through for more than the human's spending limit. Three kettles at
    # Rs 2499 = Rs 7497, against a Rs 900 limit, must be refused.
    resp = tool_call("purchase", {"sku": "ACC-001", "quantity": 3,
                                  "max_price_paise": 90000})
    assert resp["result"]["isError"] is True
    assert "Refused" in text_of(resp)
    # And the refusal is recorded, not silent.
    assert "order.rejected" in event_kinds()


def test_purchase_within_the_limit_succeeds():
    # Prevents: an over-strict server that blocks legitimate sales. Masala chai
    # at Rs 549 against a Rs 900 limit is a good sale and must complete.
    resp = tool_call("purchase", {"sku": "TEA-003", "quantity": 1,
                                  "max_price_paise": 90000})
    assert resp["result"].get("isError") is not True
    text = text_of(resp)
    assert "Order created" in text
    assert "ord_" in text
    assert "order.created" in event_kinds()


def test_price_field_is_ignored_and_catalog_price_is_charged():
    # Prevents: the price-injection attack - a caller (or a poisoned listing
    # steering one) naming its own price. A "price": 1 must be discarded, the
    # catalog price (34900 for TEA-001) charged, and the attempt logged.
    resp = tool_call("purchase", {"sku": "TEA-001", "quantity": 1,
                                  "max_price_paise": 90000, "price": 1})
    text = text_of(resp)
    assert "34900" in text          # real catalog price, not the injected 1
    assert "mcp.price_attempt_ignored" in event_kinds()


def test_purchase_without_a_limit_is_rejected():
    # Prevents: treating a missing spending limit as "unlimited". No limit must
    # mean no purchase, never an uncapped charge.
    resp = tool_call("purchase", {"sku": "TEA-003", "quantity": 1})
    assert resp["result"]["isError"] is True
    assert "max_price_paise" in text_of(resp)
    # No order should have been created.
    assert "order.created" not in event_kinds()


def test_purchase_recreates_a_missing_database():
    # Prevents: the live crash found by hand against Claude Desktop - the MCP
    # server handling a purchase with the database FILE MISSING and dying with
    # a sqlite "no such table: orders" internal error. The server must ensure
    # its tables exist before serving a request; it must not assume some other
    # process (the FastAPI app's startup) created them.
    #
    # NOTE: this test deliberately deletes the database the fixture just
    # created. Every other test in this file relies on the fresh_db fixture,
    # which ALWAYS calls init_db() first - so a clean, valid schema is always
    # present and none of them could ever have caught this bug. That gap is the
    # whole lesson (see DECISIONS.md).
    store.DB_PATH.unlink()
    assert not store.DB_PATH.exists()

    resp = tool_call("purchase", {"sku": "TEA-003", "quantity": 1,
                                  "max_price_paise": 90000})

    # It must complete the sale, not surface an internal sqlite error.
    assert resp["result"].get("isError") is not True, text_of(resp)
    text = text_of(resp)
    assert "no such table" not in text
    assert "Order created" in text
    assert "order.created" in event_kinds()
