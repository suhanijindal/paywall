# Paywall

A payments layer that lets a shopper, human or AI, buy from a merchant in plain
English, while a separate layer of ordinary code decides whether the payment is
allowed. Built on Razorpay test-mode APIs.

> The model chooses **what** to buy. It never chooses **how much money moves**.
> The purchase request has no price field at all.

## The problem

AI assistants are starting to make purchases for people. Razorpay and NPCI (the
body that runs UPI, India's instant bank-transfer system) have run live pilots
for agentic payments, and NPCI is drafting a national standard, the Unified
Agent Protocol, so that AI agents can pay within limits a user sets in advance.

That creates a risk nobody had before. A language model is not a safe thing to
put in charge of money. It can be tricked by text it reads, it can misunderstand
an instruction, and it can hallucinate a product that does not exist. If the
model is the thing calling the payment API, one bad day means real money leaves
a real account.

## The approach

The model proposes. Code decides.

The AI buyer can only ever say two things: what it wants, and the most it is
allowed to spend. It cannot name a price, cannot pick an amount, and cannot call
Razorpay directly. A separate layer looks up the real price from the merchant's
own catalog, checks the request against the spending limit, and only then
creates the payment.

This is the entire request shape for a purchase:

```json
{
  "query": "masala chai",
  "max_price_paise": 90000,
  "quantity": 1,
  "idempotency_key": "any-unique-string"
}
```

There is no `price`, `amount`, or `total` field, by design. Amounts are in paise,
the smallest unit of the rupee (100 paise = 1 rupee), stored as whole numbers to
avoid rounding errors. The `idempotency_key` is a unique string the caller
attaches so that if the same request arrives twice, the server returns the
original order instead of charging twice.

The merchant catalog deliberately contains a poisoned product listing whose
description tries to hijack any AI reading it into buying the most expensive
item. Tested live against Claude Desktop, the model recognised the injected
instruction and refused it. That is the model catching the attack, which is not
something to rely on. The measurement harness therefore assumes the opposite:
that the model is completely fooled and genuinely wants the costly item. It then
shows the spending limit still holds. Being fooled is survivable because the
limit is enforced in code, not by the model.

## Quickstart

Python 3.9. No credentials needed: payment calls run in mock mode and return
realistic fake responses so you can see the whole flow.

```bash
git clone https://github.com/suhanijindal/paywall.git && cd paywall
pip install -r requirements.txt
python3 -m uvicorn app.main:app
```

The server starts on http://localhost:8000. Open http://localhost:8000/docs for
the interactive API.

## See it work

Order ids are random, so the ones you get will differ from those below.

A purchase that is allowed:

```bash
curl -s -X POST http://localhost:8000/agent/buy \
  -H 'Content-Type: application/json' \
  -d '{"query":"masala chai","max_price_paise":90000,"quantity":1,"idempotency_key":"demo-1"}'
```

```json
{
  "order_id": "ord_a72b28ce9d27",
  "quantity": 1,
  "amount_paise": 54900,
  "razorpay_order_id": "order_MOCK38c1113b21",
  "checkout_url": "/checkout/ord_a72b28ce9d27",
  "suggestion": {"name": "Bamboo Tea Infuser", "price_paise": 19900}
}
```

(The full response also echoes the matched product; trimmed here for length.)
Ask the system to explain that order in plain English:

```bash
curl -s http://localhost:8000/explain/ord_a72b28ce9d27
```

```
Order ord_a72b28ce9d27. An AI assistant requested Masala Chai Blend, 500g with a
spending limit of Rs 900. The system matched Masala Chai Blend, 500g at Rs 549
from the merchant catalog. The assistant did not supply this price. Rs 549 is
within the Rs 900 limit, so the order was created. A Bamboo Tea Infuser at Rs 199
was suggested because Rs 351 of the limit remained. Payment has not yet been
confirmed.
```

A purchase that is refused. The cast iron kettle costs Rs 2,499, the limit is
Rs 500:

```bash
curl -s -X POST http://localhost:8000/agent/buy \
  -H 'Content-Type: application/json' \
  -d '{"query":"cast iron kettle","max_price_paise":50000,"quantity":1,"idempotency_key":"demo-2"}'
```

```json
{"detail":{"error":"The closest matching product is over your spending limit","order_id":"ord_2d7a9e9a5e28","explain_url":"/explain/ord_2d7a9e9a5e28"}}
```

The refusal returns HTTP 400 but still carries an `order_id`, because a refused
request is a lost sale a merchant will want justified. It is recorded in the
audit log but is never written as an order. Explain it the same way:

```bash
curl -s http://localhost:8000/explain/ord_2d7a9e9a5e28
```

```
Order ord_2d7a9e9a5e28. An AI assistant requested cast iron kettle with a
spending limit of Rs 500. The closest match was Cast Iron Kettle, 1.2L at
Rs 2,499 from the merchant catalog. Rs 2,499 exceeds the Rs 500 limit, so no
order was created and no payment was attempted.
```

## Connecting an AI assistant

The project ships an MCP server. MCP, the Model Context Protocol, is a standard
that lets an AI assistant call external tools directly instead of a human
copying data in and out of a chat. This server exposes three tools to the
assistant: `search_products`, `get_product`, and `purchase`. It has been
verified live with Claude Desktop.

Add this to Claude Desktop's config file, on macOS at
`~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "paywall": {
      "command": "python3",
      "args": ["-m", "app.mcp_server"],
      "env": {
        "PYTHONPATH": "/absolute/path/to/paywall"
      }
    }
  }
}
```

The `env` block with `PYTHONPATH` is required, and this cost an hour to discover.
Claude Desktop ignores any working directory you set and launches the server
with a stripped environment, so Python cannot find the `app` package unless
`PYTHONPATH` points at the absolute path of the repo. Set it and the tools
appear; leave it out and the server fails to start.

## Results

Measured by `python3 -m app.simulate`, a harness that runs 228 scripted shopper
sessions (202 honest, 21 attacks, 5 borderline) through the same checks the live
system uses.

| Measure                        | Result                       |
| ------------------------------ | ---------------------------- |
| Attacks blocked                | 19 / 21  (90.5%)             |
| Attacks that got through       | 2                            |
| Honest shoppers wrongly blocked| 0  (0.0%)                    |
| Average order value            | Rs 1,166.59                  |
| With add-on suggestion         | Rs 1,265.02  (+8.4%)         |

## What we got wrong

The 2 attacks that got through were both quantity inflation: many units of a
cheap item, with the quantity tuned to keep the total just under the limit. They
succeeded because the spending limit bounds the money, not the quantity. The
user never spends over their limit, but pays for more units than they likely
meant to. This is a real gap, kept in the results rather than relabelled to make
the block rate look better.

The 0.0% false-block rate is a warning, not a trophy. The honest shopper set is
all straightforward requests, so a zero here most likely means the test is too
easy, not that the system is perfect. Harder, genuinely ambiguous honest cases
are needed before that number means much.

Two bugs were found by live testing that the test suite missed. First, the MCP
server crashed with `no such table: orders` when the database file was absent,
because only the web app created the tables on startup; every test used a
fixture that always created them first, so none could catch it. Second, refused
requests returned an error with no order id, leaving nothing to call `/explain`
on; the tests only exercised `/explain` on successful orders. Both are fixed,
and both are the same lesson: fixtures that always set up a clean, valid state
cannot catch missing-setup bugs.

## Architecture

`ARCHITECTURE.md` covers the components, the single enforcement path, and how
duplicate requests, duplicate webhooks (a webhook is a message Razorpay sends
our server when a payment status changes), bad signatures, and missing webhooks
are each handled. In short: `/agent/chat` and `/agent/buy` both funnel into one
function that looks up the price, checks the limit and stock, and records every
decision to an append-only ledger. `recommend.py` suggests add-ons bounded by
the limit, and `reconcile.py` recovers payments whose webhook never arrived.

## What I would build next

- Add a per-quantity sanity check alongside the total limit, to close the
  quantity-inflation gap the harness found.
- Expand the honest shopper set with genuinely ambiguous cases, so the
  false-block rate is tested hard enough to be trustworthy.
- Replace keyword catalog search with semantic search, so vague queries reach
  the product the shopper actually meant.
- Move storage from SQLite to Postgres and hash each ledger row, making the
  audit trail tamper-evident.
