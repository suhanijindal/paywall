from __future__ import annotations
"""
The failure the track asks you to handle.

The problem: normally Razorpay tells us when a payment succeeds, by calling
our webhook. But that message can go missing. Our server might be restarting.
The network might drop it. Then we have an order stuck at "created" forever,
even though the customer actually paid.

That is bad in a specific way: the customer's money left their account and our
system thinks they owe us. They get chased for a payment they already made.

The fix: instead of waiting to be told, we go and ask. This job finds every
order still sitting at "created", asks Razorpay directly what happened to it,
and fixes our record.

This pattern has a name in payments - reconciliation. It is the safety net
under every webhook, because webhooks are best-effort and money is not.
"""

import asyncio
import time

from app import payments, store

# How long to wait before treating a "created" order as suspicious.
# Real payments finish in seconds; anything older than this should have
# been confirmed already.
STALE_AFTER_SECONDS = 60


async def reconcile_once() -> dict:
    """
    Check every stuck order against Razorpay. Returns a summary of what it did.

    Safe to run repeatedly. Orders already marked paid are skipped, so running
    it twice does not double-process anything.
    """
    checked = 0
    fixed = 0
    still_unpaid = 0
    now = time.time()

    for order in store.list_orders(limit=500):
        if order["status"] != "created":
            continue
        if now - order["created_at"] < STALE_AFTER_SECONDS:
            continue  # too fresh, give the webhook a chance

        checked += 1
        try:
            remote = await payments.fetch_order(order["razorpay_order_id"])
        except Exception as exc:
            store.log_event("reconcile.error", {
                "razorpay_order_id": order["razorpay_order_id"],
                "error": str(exc),
            }, order_id=order["id"])
            continue

        # Razorpay marks an order "paid" once the full amount is captured.
        if remote.get("status") == "paid":
            payment_id = remote.get("_payment_id", "recovered_by_reconciliation")
            store.mark_paid(order["id"], payment_id)
            store.log_event("reconcile.recovered", {
                "reason": "Razorpay says paid but no webhook arrived",
                "razorpay_order_id": order["razorpay_order_id"],
            }, order_id=order["id"])
            fixed += 1
        else:
            still_unpaid += 1

    summary = {"checked": checked, "recovered": fixed, "still_unpaid": still_unpaid}
    if checked:
        store.log_event("reconcile.run", summary)
    return summary


async def run_forever(interval_seconds: int = 30) -> None:
    """Background loop. Started when the app boots."""
    while True:
        try:
            await reconcile_once()
        except Exception as exc:
            store.log_event("reconcile.crashed", {"error": str(exc)})
        await asyncio.sleep(interval_seconds)
