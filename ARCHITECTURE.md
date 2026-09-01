# Architecture

Written so that someone who has never seen this code can understand what it does and why it does it that way.

---

## 1. What this system is

A merchant sells things. An AI assistant wants to buy something on behalf of a person. This system sits between them.

Its job is to make the merchant **purchasable by a machine** while making sure that no single mistake by that machine can cost the merchant or the customer money.

```
   AI buyer                     Paywall                     Razorpay
      |                            |                            |
      |  "chai, up to Rs 600"      |                            |
      |--------------------------->|                            |
      |                            | look up the real price     |
      |                            | check the limit            |
      |                            | check stock                |
      |                            | check for a duplicate       |
      |                            |                            |
      |                            |  create Order              |
      |                            |--------------------------->|
      |                            |<---------------------------|
      |  order + checkout link     |                            |
      |<---------------------------|                            |
      |                            |                            |
                          [ human approves ]                    |
                                   |                            |
                                   |<--- signed webhook --------|
                                   | verify signature           |
                                   | mark paid                  |
                                   | write to ledger            |
```

---

## 2. The one rule everything else follows

> **The language model never touches money.**

The AI buyer is allowed to send exactly this:

```json
{
  "query": "masala chai",
  "max_price_paise": 60000,
  "quantity": 1,
  "idempotency_key": "buy-001"
}
```

Notice what is missing: **there is no price field.**

The buyer says what it wants and the ceiling it may spend under. It cannot state an amount. The price is looked up server-side from the merchant's own catalog.

This is not a small detail. It is the difference between a system where a confused or manipulated model can pay ₹1 for a ₹2,499 kettle, and one where it structurally cannot. The worst a compromised buyer can do is request the wrong product within the user's own limit.

---

## 3. Components

### `app/catalog.py` — what the merchant sells

A product list with prices in **paise**, not rupees.

Money is stored as whole numbers throughout. `34900` means ₹349.00. This is standard in payments because decimal arithmetic on floating-point numbers produces rounding errors, and rounding errors in money are unacceptable. Razorpay's own API takes amounts in paise for the same reason.

Right now the catalog is a hardcoded Python list. That is deliberate: the payment path is the risky part of the project, so it gets built and proven first. The catalog gets replaced with a real database and an ingestion pipeline later, and nothing else has to change because everything reads it through two functions.

### `app/payments.py` — everything Razorpay

All Razorpay communication is isolated in one file behind a handful of functions. The rest of the application never knows which payment provider is in use.

**Mock mode.** If no API keys are present, these functions return realistically-shaped fake responses instead of calling out to the internet. This means someone can clone the repo and run the whole system immediately with no credentials. Real mode activates the moment keys exist. No code changes, no flags to flip.

**Webhook signature verification.** Razorpay signs every webhook with a shared secret using HMAC-SHA256. We recompute that signature and compare using a constant-time comparison. Skipping this check is the most common serious mistake in payment integrations — without it, anyone who discovers the webhook URL can tell the system that unpaid orders were paid.

### `app/store.py` — data

Two tables:

**`orders`** — one row per purchase attempt and its current state.

**`events`** — the append-only log. Rows are only ever inserted, never updated or deleted. That property is what makes it an audit trail rather than a status table. Every meaningful action writes here: orders created, orders refused (with the reason), duplicates caught, webhooks received, webhooks rejected, payments captured.

SQLite for now. It is a single file, needs no setup, and someone cloning the repo gets a working system in one command. Migrating to PostgreSQL means rewriting this one file, because it is the only place that knows SQL.

### `app/main.py` — the HTTP layer

The endpoints. Thin by design — it validates input, calls the other modules, and formats responses.

---

## 4. Failure handling

Payments fail in ordinary, predictable ways. Handling them is not optional polish; it is the product.

### Duplicate requests

Every purchase request carries an **idempotency key** — a unique string chosen by the caller. Before doing anything, we check whether that key has been seen. If it has, we return the original order rather than creating a second one.

This matters because networks are unreliable. A request succeeds, the response gets lost, the caller assumes failure and retries. Without idempotency that is two orders and two charges. With it, it is one order returned twice.

### Duplicate webhooks

Razorpay retries webhooks if it does not get a clean response. If we receive a confirmation for an order already marked paid, we log it as a duplicate and do nothing further. Processing it twice would corrupt the record.

### Webhooks for unknown orders

If a webhook arrives referencing an order we have no record of, we log it as orphaned and return successfully rather than crashing. Returning an error would make Razorpay retry it forever.

### Bad signatures

Rejected outright and written to the ledger. An unexplained cluster of these entries is a security signal.

### Missing webhooks

If our server was down when Razorpay called, the confirmation is simply lost. `fetch_order()` exists to ask Razorpay directly what it thinks the status is. The reconciliation job that uses it on a schedule is the next piece of work.

---

## 5. Trust boundaries

Who is allowed to be believed about what:

| Source | Trusted for | Not trusted for |
|---|---|---|
| AI buyer | what the user wants, the spending limit | prices, amounts, product availability |
| Merchant catalog | prices, stock | anything, once it contains text written by third parties |
| Browser | nothing about payment status | — |
| Razorpay webhook, signature verified | payment status | — |

The middle row becomes important in the next phase. A product description is text from outside the system, and text from outside the system can carry instructions aimed at the AI buyer. Defending against that is why the limit checks are code and not prompts.

---

## 6. What comes next

1. **Reconciliation job** — periodically ask Razorpay about orders stuck in `created` and resolve them
2. **Signed mandates** — the user cryptographically signs their spending limits, so the limits themselves cannot be forged
3. **Tamper-evident ledger** — each event carries a hash of the previous one, so altering history becomes detectable
4. **Real catalog ingestion** — replace the hardcoded list with products read from a merchant's actual site
5. **Adversarial test suite** — including a product description containing a prompt injection, to demonstrate that the limit checks hold when the AI buyer is actively deceived
