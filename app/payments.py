from __future__ import annotations
"""
Everything that talks to Razorpay lives here.

Why one file: if all the payment-provider code sits behind a small set of
functions, the rest of the app never needs to know which provider we use.
That is a normal, defensible architecture choice and it is easy to explain
in an interview.

MOCK MODE
---------
If no Razorpay keys are set in the environment, we run in mock mode: the
functions return fake but realistically-shaped responses. This means the
project runs on a laptop with no credentials, which matters when someone
else clones the repo to try it. Real mode switches on the moment keys exist.
"""

import base64
import hashlib
import hmac
import os
import uuid

import httpx

RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")

API_BASE = "https://api.razorpay.com/v1"

MOCK_MODE = not (RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET)


def _auth_header() -> dict:
    """
    Razorpay uses HTTP Basic Auth: the key id and secret joined by a colon,
    base64-encoded, sent in the Authorization header.
    """
    raw = f"{RAZORPAY_KEY_ID}:{RAZORPAY_KEY_SECRET}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


async def create_order(amount_paise: int, receipt: str, notes: dict) -> dict:
    """
    Ask Razorpay to create an Order.

    An Order is Razorpay's record of "this merchant expects this much money
    for this thing". It exists before any money moves. The customer then pays
    against that order. Creating the order server-side is what stops a
    customer from editing the price in their browser before paying.
    """
    if MOCK_MODE:
        return {
            "id": "order_MOCK" + uuid.uuid4().hex[:10],
            "amount": amount_paise,
            "currency": "INR",
            "receipt": receipt,
            "status": "created",
            "notes": notes,
            "_mock": True,
        }

    payload = {
        "amount": amount_paise,
        "currency": "INR",
        "receipt": receipt,
        "notes": notes,
        # payment_capture=1 means Razorpay automatically takes the money once
        # the customer authorises it, instead of us having to call capture.
        "payment_capture": 1,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(f"{API_BASE}/orders", json=payload, headers=_auth_header())
        resp.raise_for_status()
        return resp.json()


async def fetch_order(razorpay_order_id: str) -> dict:
    """
    Ask Razorpay what it thinks the status of an order is.

    We need this for reconciliation: if a webhook never arrives (network
    problems, our server was down), we can still ask Razorpay directly rather
    than leaving the order stuck forever.
    """
    if MOCK_MODE:
        return {"id": razorpay_order_id, "status": "created", "_mock": True}

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{API_BASE}/orders/{razorpay_order_id}", headers=_auth_header())
        resp.raise_for_status()
        return resp.json()


def verify_webhook_signature(raw_body: bytes, received_signature: str) -> bool:
    """
    Check that a webhook actually came from Razorpay and not from someone
    pretending to be Razorpay.

    Razorpay signs each webhook with a shared secret using HMAC-SHA256.
    We recompute the signature ourselves and compare. If we skipped this,
    anyone who knew our webhook URL could tell our system that unpaid orders
    were paid. This is the single most common security mistake in payment
    integrations, so it is worth calling out in the pitch.
    """
    if MOCK_MODE and not RAZORPAY_WEBHOOK_SECRET:
        return True  # mock mode only, never in production

    expected = hmac.new(
        RAZORPAY_WEBHOOK_SECRET.encode(),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    # compare_digest compares in constant time, which prevents an attacker
    # from guessing the signature one character at a time by timing us.
    return hmac.compare_digest(expected, received_signature or "")
