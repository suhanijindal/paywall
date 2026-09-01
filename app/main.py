from __future__ import annotations
"""
The web server.

This is the front door of the system. Everything else in the project is
called from here.

Endpoints, in the order the story happens:

  GET  /catalog              what the merchant sells
  POST /agent/buy            an AI buyer asks to purchase something
  GET  /checkout/{order_id}  a page where the human actually pays
  POST /webhooks/razorpay    Razorpay tells us the payment succeeded
  GET  /orders               every purchase and its status
  GET  /ledger               the audit trail
"""

import asyncio
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app import buyer_agent, catalog, payments, recommend, reconcile, store


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Startup and shutdown, expressed the way current FastAPI wants it.

    This replaces the deprecated @app.on_event("startup") hook; the behaviour
    is identical. On startup: create the database tables, record that the
    system started, and launch the background reconciliation loop that catches
    any payment whose webhook never arrived. On shutdown we cancel that loop.
    """
    store.init_db()
    store.log_event("system.started", {"mock_mode": payments.MOCK_MODE})
    reconcile_task = asyncio.create_task(reconcile.run_forever())
    try:
        yield
    finally:
        reconcile_task.cancel()


app = FastAPI(title="Paywall", version="0.1.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

@app.get("/catalog")
def get_catalog():
    """The merchant's full product list, in a form a machine can read."""
    return {"products": catalog.CATALOG}


# ---------------------------------------------------------------------------
# Discovery - the front door for an outside AI agent
# ---------------------------------------------------------------------------

@app.get("/.well-known/agentic-commerce.json")
def agentic_commerce_manifest(request: Request):
    """
    The one document an outside AI agent reads to learn how to buy from us.

    Track 1 asks us to make the merchant "sellable to AI buyers". This is how:
    a well-known URL any agent (Claude Desktop, ChatGPT, a custom bot) can
    fetch to discover the catalog, the purchase endpoint, and - most
    importantly - the rules. An agent that reads this should be able to buy
    with no other documentation.

    The "constraints" block is the whole safety design stated in plain words
    for a machine: you MUST tell us your spending limit, and you CANNOT tell us
    a price. Prices come from the merchant. This is not a suggestion the agent
    may ignore; the /agent/buy request shape has no price field to fill in.
    """
    base = str(request.base_url).rstrip("/")
    return {
        "schema_version": "0.1",
        "merchant": {
            "name": "Chai & Coffee Co.",
            "description": "An Indian tea and coffee shop that sells to AI buyers.",
        },
        "catalog_url": base + "/catalog",
        "purchase_endpoint": {
            "url": base + "/agent/buy",
            "method": "POST",
            "content_type": "application/json",
            "request_shape": {
                "query": "string - what you want, in plain words (e.g. 'masala chai')",
                "max_price_paise": "integer - REQUIRED - the most you may spend, in paise",
                "quantity": "integer 1-10 - how many units",
                "idempotency_key": "string - a unique id for this purchase attempt",
            },
            "returns": "The matched product, the price we looked up, a Razorpay "
                       "order, and a checkout_url a human opens to approve payment.",
        },
        "constraints": {
            "max_price_paise": {
                "required": True,
                "note": "You MUST supply this. It is your spending limit, applied "
                        "to the order total. No limit means no purchase.",
            },
            "price": {
                "accepted": False,
                "note": "You CANNOT supply a price, amount, or total. Prices come "
                        "from the merchant's catalog and are looked up on our side. "
                        "The request shape above has no field for a price by design.",
            },
        },
        "mcp": {
            "note": "This merchant also ships a Model Context Protocol server "
                    "(app/mcp_server.py) so an AI assistant can search and buy "
                    "using tools instead of raw HTTP. See the README.",
        },
    }


# ---------------------------------------------------------------------------
# The buying flow
# ---------------------------------------------------------------------------

class BuyRequest(BaseModel):
    """What an AI buyer sends us when it wants to purchase something."""
    query: str = Field(..., description="What the buyer is looking for, in plain words")
    max_price_paise: int = Field(..., description="The most it is allowed to spend, in paise")
    quantity: int = Field(1, ge=1, le=10)
    idempotency_key: str = Field(..., description="Unique string for this purchase attempt")


async def create_purchase(product, quantity, max_price_paise, idempotency_key):
    """
    The one and only place an order is created and the limit is enforced.

    Both the HTTP endpoint (/agent/buy) and the MCP server (app/mcp_server.py)
    call this. There is deliberately no second path: a purchase that skips
    these checks cannot exist, because there is nowhere else that creates an
    order. The caller resolves *which* product it wants; this function decides
    whether the sale is allowed and, if so, prices and records it.

    It never raises for a business refusal. Instead it returns a dict with an
    "outcome" the caller translates into whatever its channel needs - an HTTP
    status code for the API, plain English for an AI assistant. The outcomes:

        over_limit  the total is more than the caller is allowed to spend
        no_stock    fewer units exist than were asked for
        created     the order was made; payment can now happen

    Note what the caller does NOT get to pass: a price. The amount is computed
    here from the catalog, every time. A compromised buyer can choose the wrong
    product; it can never choose what that product costs.
    """
    amount = product["price_paise"] * quantity

    # Give every purchase attempt an id up front - refused or not - so its whole
    # story can be retold later by /explain. The id is attached to the events
    # below even when no row is written to the orders table: a refused attempt
    # never becomes an order, but it stays auditable.
    order_id = "ord_" + uuid.uuid4().hex[:12]

    # The limit applies to the total, not the unit price. Two kettles at the
    # limit is still over the limit.
    if amount > max_price_paise:
        store.log_event("order.rejected", {
            "reason": "total exceeds the spending limit",
            "sku": product["sku"],
            "name": product["name"],
            "quantity": quantity,
            "total_paise": amount,
            "limit_paise": max_price_paise,
        }, order_id=order_id)
        return {"outcome": "over_limit", "order_id": order_id, "sku": product["sku"],
                "amount_paise": amount, "limit_paise": max_price_paise}

    if product["stock"] < quantity:
        store.log_event("order.rejected", {
            "reason": "not enough stock",
            "sku": product["sku"],
            "name": product["name"],
            "quantity": quantity,
            "stock": product["stock"],
            "limit_paise": max_price_paise,
        }, order_id=order_id)
        return {"outcome": "no_stock", "order_id": order_id, "sku": product["sku"],
                "stock": product["stock"], "quantity": quantity}

    rzp = await payments.create_order(
        amount_paise=amount,
        receipt=order_id,
        notes={"sku": product["sku"], "quantity": str(quantity)},
    )

    store.create_order(
        order_id=order_id,
        idempotency_key=idempotency_key,
        sku=product["sku"],
        quantity=quantity,
        amount_paise=amount,
        razorpay_order_id=rzp["id"],
    )
    store.log_event("order.created", {
        "sku": product["sku"],
        "name": product["name"],
        "quantity": quantity,
        "amount_paise": amount,
        "limit_paise": max_price_paise,
        "razorpay_order_id": rzp["id"],
        "mock": rzp.get("_mock", False),
    }, order_id=order_id)

    # Revenue side: offer one add-on, but only if it still fits the limit.
    suggestion = recommend.suggest(product["sku"], amount, max_price_paise)
    if suggestion:
        store.log_event("suggestion.offered", suggestion, order_id=order_id)
    else:
        store.log_event("suggestion.withheld", {
            "reason": "no add-on fits inside the remaining spending limit",
            "headroom_paise": max_price_paise - amount,
        }, order_id=order_id)

    return {
        "outcome": "created",
        "order_id": order_id,
        "product": product,
        "quantity": quantity,
        "amount_paise": amount,
        "razorpay_order_id": rzp["id"],
        "checkout_url": f"/checkout/{order_id}",
        "suggestion": suggestion,
    }


@app.post("/agent/buy")
async def agent_buy(req: BuyRequest):
    """
    The core endpoint. An AI buyer says what it wants and how much it may
    spend; we pick a product and create a Razorpay order for it.

    Two things to notice, because they are the interview answers:

    1. The buyer never tells us a price. It tells us a *limit*. We look the
       real price up ourselves from our own catalog. A buyer that could name
       its own price could pay one rupee for a kettle.

    2. We check the idempotency key first. If this exact request already
       created an order, we return that same order instead of making a new
       one. Retried requests must never mean two charges.
    """
    existing = store.find_order_by_idempotency_key(req.idempotency_key)
    if existing:
        store.log_event(
            "order.deduplicated",
            {"reason": "idempotency key already used", "key": req.idempotency_key},
            order_id=existing["id"],
        )
        return {"order": existing, "deduplicated": True}

    matches = catalog.search(req.query, max_price_paise=req.max_price_paise)
    if not matches:
        # Nothing fits within the limit. A refusal is the case a merchant most
        # wants justified - it is a lost sale - so we mint an order_id and
        # record the refusal in the events table (never a row in orders: it was
        # never an order). To explain WHY, we look again with no price ceiling:
        # either the item exists but costs more than the limit, or there is no
        # such product at all.
        order_id = "ord_" + uuid.uuid4().hex[:12]
        closest = catalog.search(req.query)
        if closest:
            product = closest[0]
            amount = product["price_paise"] * req.quantity
            store.log_event("order.rejected", {
                "reason": "total exceeds the spending limit",
                "sku": product["sku"],
                "name": product["name"],
                "query": req.query,
                "quantity": req.quantity,
                "total_paise": amount,
                "limit_paise": req.max_price_paise,
            }, order_id=order_id)
            raise HTTPException(400, detail={
                "error": "The closest matching product is over your spending limit",
                "order_id": order_id,
                "explain_url": f"/explain/{order_id}",
            })
        store.log_event("order.rejected", {
            "reason": "no product matched the request",
            "query": req.query,
            "limit_paise": req.max_price_paise,
        }, order_id=order_id)
        raise HTTPException(404, detail={
            "error": "No product matches that request",
            "order_id": order_id,
            "explain_url": f"/explain/{order_id}",
        })

    result = await create_purchase(
        matches[0], req.quantity, req.max_price_paise, req.idempotency_key
    )

    if result["outcome"] == "over_limit":
        raise HTTPException(400, detail={
            "error": "Total exceeds the spending limit",
            "order_id": result["order_id"],
            "explain_url": f"/explain/{result['order_id']}",
        })
    if result["outcome"] == "no_stock":
        raise HTTPException(409, detail={
            "error": "Not enough stock",
            "order_id": result["order_id"],
            "explain_url": f"/explain/{result['order_id']}",
        })

    return {
        "order_id": result["order_id"],
        "product": result["product"],
        "quantity": result["quantity"],
        "amount_paise": result["amount_paise"],
        "razorpay_order_id": result["razorpay_order_id"],
        "checkout_url": result["checkout_url"],
        "suggestion": result["suggestion"],
    }


@app.get("/checkout/{order_id}", response_class=HTMLResponse)
def checkout_page(order_id: str):
    """
    A minimal page where a human completes the payment.

    Even in an agent-driven purchase there is a moment where a person
    approves the money leaving their account. This page is that moment.
    It loads Razorpay's own checkout widget, so card details never touch
    our server - that is deliberate and it is what keeps us out of scope
    for a lot of payment compliance requirements.
    """
    order = store.get_order(order_id)
    if not order:
        raise HTTPException(404, "Unknown order")

    product = catalog.BY_SKU[order["sku"]]
    key_id = payments.RAZORPAY_KEY_ID or "MOCK_KEY"
    rupees = order["amount_paise"] / 100

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Approve payment</title>
<style>
 body {{ font-family: system-ui, sans-serif; max-width: 30rem; margin: 4rem auto; padding: 0 1rem; }}
 .box {{ border: 1px solid #ddd; border-radius: 10px; padding: 1.5rem; }}
 .amount {{ font-size: 2rem; font-weight: 600; }}
 button {{ background: #0b5fff; color: #fff; border: 0; padding: .8rem 1.4rem;
           border-radius: 8px; font-size: 1rem; cursor: pointer; }}
 .muted {{ color: #666; font-size: .9rem; }}
</style></head>
<body>
  <div class="box">
    <p class="muted">An AI assistant prepared this purchase for your approval.</p>
    <h2>{product['name']}</h2>
    <p class="muted">Quantity {order['quantity']} &middot; {order['sku']}</p>
    <p class="amount">&#8377;{rupees:,.2f}</p>
    <button onclick="pay()">Approve and pay</button>
    <p class="muted" style="margin-top:1.5rem">Order {order_id}</p>
  </div>
<script src="https://checkout.razorpay.com/v1/checkout.js"></script>
<script>
function pay() {{
  var options = {{
    key: "{key_id}",
    order_id: "{order['razorpay_order_id']}",
    amount: {order['amount_paise']},
    currency: "INR",
    name: "Chai & Coffee Co.",
    description: "{product['name']}",
    handler: function (response) {{
      document.body.innerHTML =
        "<div class='box'><h2>Payment submitted</h2>" +
        "<p class='muted'>Payment id: " + response.razorpay_payment_id + "</p>" +
        "<p class='muted'>The system will confirm this independently via webhook.</p></div>";
    }}
  }};
  new Razorpay(options).open();
}}
</script>
</body></html>"""


# ---------------------------------------------------------------------------
# Webhook - Razorpay calling us
# ---------------------------------------------------------------------------

@app.post("/webhooks/razorpay")
async def razorpay_webhook(request: Request):
    """
    Razorpay calls this URL to tell us a payment happened.

    Important detail worth saying out loud in the pitch: we do NOT trust the
    browser's word that payment succeeded. The browser can be manipulated.
    We only mark an order paid when Razorpay's own servers tell us so, and
    only after we verify the signature on that message.
    """
    raw = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    if not payments.verify_webhook_signature(raw, signature):
        store.log_event("webhook.rejected", {"reason": "bad signature"})
        raise HTTPException(400, "Invalid signature")

    body = await request.json()
    event = body.get("event", "")
    store.log_event("webhook.received", {"event": event})

    if event in ("payment.captured", "order.paid"):
        entity = body["payload"]["payment"]["entity"]
        rzp_order_id = entity.get("order_id")
        payment_id = entity.get("id")

        order = store.get_order_by_razorpay_id(rzp_order_id)
        if not order:
            store.log_event("webhook.orphaned", {"razorpay_order_id": rzp_order_id})
            return {"status": "ignored"}

        if order["status"] == "paid":
            # Razorpay retries webhooks. Receiving the same one twice must
            # not do the work twice.
            store.log_event("webhook.duplicate", {"payment_id": payment_id},
                            order_id=order["id"])
            return {"status": "already processed"}

        store.mark_paid(order["id"], payment_id)
        store.log_event("payment.captured", {
            "payment_id": payment_id,
            "amount_paise": entity.get("amount"),
        }, order_id=order["id"])

    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Visibility
# ---------------------------------------------------------------------------

@app.get("/orders")
def orders():
    return {"orders": store.list_orders()}


@app.get("/ledger")
def ledger():
    """Everything the system has done, newest first."""
    return {"events": store.list_events()}


# ---------------------------------------------------------------------------
# Explainability - turning the machine-readable audit trail into English
# ---------------------------------------------------------------------------

def _rupees(paise) -> str:
    """Format a paise amount as a rupee string, dropping a trailing .00."""
    try:
        value = paise / 100
    except TypeError:
        return str(paise)
    if value == int(value):
        return "Rs {:,}".format(int(value))
    return "Rs {:,.2f}".format(value)


def build_order_narrative(order_id: str):
    """
    Retell everything that happened to one order in plain English, read purely
    from the append-only events table. Returns the narrative string, or None if
    the id is unknown so the endpoint can answer 404.

    Every money action logs an event with a machine-readable reason code; this
    is what turns that trail into a sentence a human can check. It handles a
    refused attempt as well as a completed order.
    """
    events = store.events_for_order(order_id)
    if not events and store.get_order(order_id) is None:
        return None

    by_kind = {}
    for e in events:
        by_kind.setdefault(e["kind"], []).append(e["detail"])

    parts = ["Order {}.".format(order_id)]

    if by_kind.get("order.created"):
        d = by_kind["order.created"][0]
        name = d.get("name", d.get("sku", "the item"))
        qty = d.get("quantity", 1)
        amount = d.get("amount_paise", 0)
        limit = d.get("limit_paise")
        asked = name if not qty or qty == 1 else "{} × {}".format(qty, name)

        if limit is not None:
            parts.append("An AI assistant requested {} with a spending limit of {}."
                         .format(asked, _rupees(limit)))
        else:
            parts.append("An AI assistant requested {}.".format(asked))
        parts.append("The system matched {} at {} from the merchant catalog — "
                     "the assistant did not supply this price.".format(name, _rupees(amount)))
        if limit is not None:
            parts.append("{} is within the {} limit, so the order was created."
                         .format(_rupees(amount), _rupees(limit)))
        else:
            parts.append("The order was created.")

        if by_kind.get("suggestion.offered"):
            sug = by_kind["suggestion.offered"][0]
            remained = (limit - amount) if limit is not None else None
            if remained is not None:
                parts.append("A {} at {} was suggested because {} of the limit remained."
                             .format(sug.get("name", "an add-on"),
                                     _rupees(sug.get("price_paise", 0)), _rupees(remained)))
            else:
                parts.append("A {} at {} was suggested as an add-on."
                             .format(sug.get("name", "an add-on"),
                                     _rupees(sug.get("price_paise", 0))))
        elif by_kind.get("suggestion.withheld"):
            wh = by_kind["suggestion.withheld"][0]
            parts.append("No add-on was suggested because only {} of the limit "
                         "remained — not enough for one."
                         .format(_rupees(wh.get("headroom_paise", 0))))

        if by_kind.get("order.deduplicated"):
            parts.append("A later identical request reusing the same idempotency key "
                         "was recognised as a duplicate, so no second order was created.")

        if by_kind.get("payment.captured"):
            pay = by_kind["payment.captured"][0]
            parts.append("Payment has been confirmed (payment id {})."
                         .format(pay.get("payment_id", "unknown")))
        else:
            parts.append("Payment has not yet been confirmed.")

    elif by_kind.get("order.rejected"):
        d = by_kind["order.rejected"][-1]
        reason = d.get("reason", "the request could not be fulfilled")
        sku = d.get("sku")
        name = d.get("name") or (catalog.BY_SKU.get(sku, {}).get("name") if sku else None)
        query = d.get("query")
        limit = d.get("limit_paise")
        total = d.get("total_paise")
        qty = d.get("quantity")
        # What the shopper asked for: the raw query if we kept it, else the
        # product name we resolved it to.
        requested = query or name or "a product"

        if limit is not None:
            parts.append("An AI assistant requested {} with a spending limit of {}."
                         .format(requested, _rupees(limit)))
        else:
            parts.append("An AI assistant requested {}.".format(requested))

        if reason == "total exceeds the spending limit":
            match = name or "the item"
            if qty and qty != 1:
                parts.append("The closest match was {} × {} at {} in total from "
                             "the merchant catalog.".format(qty, match, _rupees(total)))
            else:
                parts.append("The closest match was {} at {} from the merchant "
                             "catalog.".format(match, _rupees(total)))
            parts.append("{} exceeds the {} limit, so no order was created and no "
                         "payment was attempted.".format(_rupees(total), _rupees(limit)))
        elif reason == "not enough stock":
            have = d.get("stock")
            tail = " (only {} in stock)".format(have) if have is not None else ""
            parts.append("The merchant did not have enough stock to fulfil it{}, so "
                         "no order was created and no payment was attempted.".format(tail))
        elif reason == "no product matched the request":
            parts.append("No product in the merchant catalog matched that request, "
                         "so no order was created and no payment was attempted.")
        else:
            parts.append("The request was refused ({}), so no order was created and "
                         "no payment was attempted.".format(reason))
    else:
        parts.append("The order was recorded, but no create or refuse event was "
                     "found for it, so there is nothing further to explain.")

    return " ".join(parts)


@app.get("/explain/{order_id}")
def explain_order(order_id: str):
    """
    A plain-English account of everything that happened to one order.

    Track 1's bar is that every money action must be EXPLAINABLE. The events
    table already records each decision as a machine-readable reason code; this
    endpoint turns that trail into something a human can read and check. It
    works for refused attempts as well as successful orders.
    """
    narrative = build_order_narrative(order_id)
    if narrative is None:
        raise HTTPException(404, "Unknown order")
    return {"order_id": order_id, "narrative": narrative}


@app.post("/admin/reconcile")
async def manual_reconcile():
    """
    Run the reconciliation sweep on demand.

    It also runs automatically in the background. This endpoint exists so the
    behaviour can be shown live in a demo instead of waiting for a timer.
    """
    return await reconcile.reconcile_once()


class ChatRequest(BaseModel):
    """A human talking normally, plus the budget they have agreed to."""
    message: str = Field(..., description="What the shopper wants, in plain English")
    max_price_paise: int = Field(..., description="The most they will spend, in paise")
    idempotency_key: str


@app.post("/agent/chat")
async def agent_chat(req: ChatRequest):
    """
    The full journey: plain English -> AI picks a product -> checks -> order.

    Read the order of operations carefully, because it is the whole argument
    of this project:

      1. The AI reads the catalog and picks a product.
         The catalog contains a listing designed to hijack it.
      2. We check the AI's answer is even a real product.
      3. WE look up the price. The AI never supplies one.
      4. We check the total against the human's limit.
      5. Only then do we create a payment.

    If the AI is fooled at step 1, steps 2 to 4 still run. That is why being
    fooled is survivable.
    """
    proposal = await buyer_agent.interpret(req.message)
    store.log_event("agent.proposed", {
        "message": req.message,
        "proposal": {k: v for k, v in proposal.items() if k != "raw_model_output"},
    })

    clean, error = buyer_agent.validate_proposal(proposal)
    if error:
        # Even a request we could not interpret is a refusal worth explaining,
        # so it gets an order_id and an events-table entry (no orders row).
        order_id = "ord_" + uuid.uuid4().hex[:12]
        store.log_event("order.rejected", {
            "reason": "could not interpret the request: " + error,
            "query": req.message,
            "limit_paise": req.max_price_paise,
        }, order_id=order_id)
        raise HTTPException(422, detail={
            "error": f"Could not act on that: {error}",
            "order_id": order_id,
            "explain_url": f"/explain/{order_id}",
        })

    if clean["model_tried_to_set_price"]:
        # Worth logging loudly. A model attempting to name a price is a strong
        # signal that something in the catalog is trying to manipulate it.
        store.log_event("agent.price_attempt_ignored", {
            "sku": clean["sku"],
            "note": "assistant included a price field; it was discarded",
        })

    # Hand over to the same gated path an ordinary request uses. No shortcuts,
    # no separate code path for AI-originated purchases.
    return await agent_buy(BuyRequest(
        query=catalog.BY_SKU[clean["sku"]]["name"],
        max_price_paise=req.max_price_paise,
        quantity=clean["quantity"],
        idempotency_key=req.idempotency_key,
    ))


@app.get("/health")
def health():
    return {"ok": True, "mock_mode": payments.MOCK_MODE}
