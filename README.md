# Paywall

**A payments layer that lets an AI assistant buy things on your behalf — without ever letting it decide how much money can move.**

Built on Razorpay test-mode APIs.

---

## The problem

AI assistants are starting to make purchases for people. Razorpay and NPCI already ran a pilot for agentic UPI payments, and NPCI is designing a national standard (the Unified Agent Protocol) so that AI agents can pay over UPI within limits the user sets in advance.

That creates a problem nobody had before. A language model is not a reliable thing to put in charge of money. It can be tricked by text it reads. It can misunderstand an instruction. It can hallucinate a product that does not exist. If that model is the thing calling the payment API, a bad day means real money leaves a real account.

## The approach

**The model proposes. Code decides.**

The AI buyer in this system can only ever say two things: *what it wants* and *the most it is allowed to spend*. It cannot name a price, cannot pick an amount, and cannot call Razorpay directly. A separate layer looks up the real price from the merchant's own catalog, checks the request against the user's limits, and only then creates the payment.

Every decision — approved or refused — gets written to an append-only log with the reason attached.

---

## Running it

You need Python 3.11+.

```bash
git clone <this repo>
cd paywall
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open http://localhost:8000/docs for the interactive API.

It runs **without Razorpay credentials** — payment calls return realistic fake responses so you can see the whole flow. To use real test-mode Razorpay:

```bash
cp .env.example .env
# fill in your test keys, then:
export $(cat .env | xargs)
uvicorn app.main:app --reload
```

Getting test keys: Razorpay Dashboard → Settings → API Keys → Generate Test Key. No business verification needed for test mode.

---

## Try it

**An AI buyer asks for masala chai, allowed to spend up to ₹600:**

```bash
curl -X POST localhost:8000/agent/buy \
  -H 'Content-Type: application/json' \
  -d '{"query":"masala chai","max_price_paise":60000,"quantity":1,"idempotency_key":"buy-001"}'
```

Returns the matched product, the real price from the catalog, a Razorpay order, and a checkout link.

**Fire the exact same request again** (simulating a network retry):

```bash
# same command, same idempotency_key
```

Returns `"deduplicated": true` and the *original* order. One request, one charge, no matter how many times it arrives.

**Ask for something outside the limit:**

```bash
curl -X POST localhost:8000/agent/buy \
  -H 'Content-Type: application/json' \
  -d '{"query":"cast iron kettle","max_price_paise":50000,"quantity":1,"idempotency_key":"buy-002"}'
```

The kettle costs ₹2,499. The limit is ₹500. Refused, with the reason recorded.

**See everything that happened:**

```bash
curl localhost:8000/ledger
```

---

## How the money flow actually works

1. AI buyer calls `/agent/buy` with a description and a spending limit
2. We search our own catalog and read the **real** price from it
3. We check: does the total fit the limit? Is there stock? Has this exact request already been processed?
4. We ask Razorpay to create an **Order** — Razorpay's record of what is owed
5. The user opens the checkout page and approves the payment
6. Razorpay's servers send us a signed **webhook** confirming the payment
7. We verify that signature before believing it, then mark the order paid

Step 7 matters. We never mark an order paid because the browser said so — a browser can be manipulated. Only Razorpay's own signed message counts.

---

## Endpoints

| Method | Path | What it does |
|---|---|---|
| GET | `/catalog` | The merchant's products, machine-readable |
| POST | `/agent/buy` | An AI buyer requests a purchase |
| GET | `/checkout/{order_id}` | Page where a human approves payment |
| POST | `/webhooks/razorpay` | Razorpay confirms a payment |
| GET | `/orders` | All purchases and their status |
| GET | `/ledger` | The audit trail |
| GET | `/health` | Is the service up, and is it in mock mode |

---

## Status

Day 1 of build. Working end to end: catalog → agent request → limit checks → Razorpay order → checkout → webhook confirmation → audit log.

See `ARCHITECTURE.md` for the design and `DECISIONS.md` for what broke along the way.
