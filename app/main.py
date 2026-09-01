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

import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app import buyer_agent, catalog, payments, recommend, reconcile, store

app = FastAPI(title="Paywall", version="0.1.0")


@app.on_event("startup")
async def startup() -> None:
    store.init_db()
    store.log_event("system.started", {"mock_mode": payments.MOCK_MODE})
    # The reconciliation loop runs in the background for the whole life of
    # the process, catching any payment whose webhook never arrived.
    import asyncio
    asyncio.create_task(reconcile.run_forever())


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

    # The limit applies to the total, not the unit price. Two kettles at the
    # limit is still over the limit.
    if amount > max_price_paise:
        store.log_event("order.rejected", {
            "reason": "total exceeds the spending limit",
            "sku": product["sku"],
            "total_paise": amount,
            "limit_paise": max_price_paise,
        })
        return {"outcome": "over_limit", "sku": product["sku"],
                "amount_paise": amount, "limit_paise": max_price_paise}

    if product["stock"] < quantity:
        store.log_event("order.rejected", {
            "reason": "not enough stock",
            "sku": product["sku"],
        })
        return {"outcome": "no_stock", "sku": product["sku"],
                "stock": product["stock"], "quantity": quantity}

    order_id = "ord_" + uuid.uuid4().hex[:12]
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
        store.log_event("order.rejected", {
            "reason": "no product matched within the spending limit",
            "query": req.query,
            "max_price_paise": req.max_price_paise,
        })
        raise HTTPException(404, "No product matches that request within the limit")

    result = await create_purchase(
        matches[0], req.quantity, req.max_price_paise, req.idempotency_key
    )

    if result["outcome"] == "over_limit":
        raise HTTPException(400, "Total exceeds the spending limit")
    if result["outcome"] == "no_stock":
        raise HTTPException(409, "Not enough stock")

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
        store.log_event("agent.rejected", {"reason": error})
        raise HTTPException(422, f"Could not act on that: {error}")

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
