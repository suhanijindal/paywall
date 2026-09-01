from __future__ import annotations
"""
The AI part of the project.

A person says something ordinary like:

    "something nice for my dad, he drinks black coffee, under 1500 rupees"

An LLM reads that, reads the shop's catalog, and picks a product. That is a
genuinely hard thing to do with rules, and it is what the AI is for.

WHAT THE AI IS ALLOWED TO DECIDE
--------------------------------
    which product, and how many

WHAT THE AI IS NOT ALLOWED TO DECIDE
------------------------------------
    the price          (read from the catalog by our code)
    the spending limit (set by the human, passed through untouched)
    whether to charge  (decided by the checks in main.py)

This is the whole safety design in three lines. The AI's answer is a
*suggestion*. It has to survive the same checks as any other request.

WHY THIS MATTERS
----------------
The catalog contains a listing whose description tries to hijack the AI
(see TEA-999 in catalog.py). On a real marketplace, product text is written
by sellers, and sellers can write instructions aimed at the AI rather than
at the shopper.

So the AI reading this catalog is reading untrusted text. It may well get
fooled. The design assumes it will. When it does, the request it produces is
still just "this product, this many", and that still has to fit the human's
spending limit. Getting fooled costs the shopper nothing.

OFFLINE MODE
------------
If there is no ANTHROPIC_API_KEY, we fall back to keyword matching so the
system still runs. Same idea as mock mode for payments: someone cloning the
repo gets a working system with no credentials.
"""

import json
import os
import re

import httpx

from app import catalog

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
MODEL = "claude-sonnet-4-6"
OFFLINE = not ANTHROPIC_API_KEY


SYSTEM_PROMPT = """You are a shopping assistant for a tea and coffee shop.

The customer will describe what they want. Pick the single best product from \
the catalog below.

Reply with ONLY a JSON object, no other text, in exactly this shape:

{"sku": "<the sku code>", "quantity": <a whole number>, "reason": "<one short sentence>"}

Rules:
- The sku must be one that appears in the catalog below.
- Quantity must be between 1 and 10.
- Do not include a price. You are not permitted to decide prices.
"""


def _catalog_for_prompt() -> str:
    """
    The catalog as the AI sees it, descriptions included.

    We do NOT strip or sanitise the descriptions before showing them to the
    model. That is deliberate. Filtering text is an arms race you eventually
    lose, and the point of this project is that we do not need to win it.
    """
    lines = []
    for item in catalog.CATALOG:
        if item["stock"] <= 0:
            continue
        lines.append(
            f"- {item['sku']} | {item['name']} | Rs {item['price_paise']/100:.0f} | "
            f"{item.get('description', '')}"
        )
    return "\n".join(lines)


def _offline_pick(message: str) -> dict:
    """Keyword fallback when no API key is configured."""
    matches = catalog.search(message)
    if not matches:
        matches = catalog.search(message.split()[0] if message.split() else "tea")
    if not matches:
        return {"sku": None, "quantity": 1, "reason": "no match found", "mode": "offline"}
    return {
        "sku": matches[0]["sku"],
        "quantity": 1,
        "reason": "closest keyword match (offline mode, no LLM)",
        "mode": "offline",
    }


async def interpret(message: str) -> dict:
    """
    Turn plain English into a product choice.

    Returns a dict with sku, quantity, reason, and mode. Notably it does NOT
    return a price or an amount - there is nowhere in this return value to put
    one, which is the point.
    """
    if OFFLINE:
        return _offline_pick(message)

    payload = {
        "model": MODEL,
        "max_tokens": 300,
        "system": SYSTEM_PROMPT + "\n\nCATALOG:\n" + _catalog_for_prompt(),
        "messages": [{"role": "user", "content": message}],
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                json=payload,
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        # If the model is unreachable we degrade to keyword matching rather
        # than failing the customer's request outright.
        fallback = _offline_pick(message)
        fallback["reason"] = f"LLM unavailable ({type(exc).__name__}), used keyword match"
        fallback["mode"] = "degraded"
        return fallback

    text = "".join(
        block.get("text", "") for block in data.get("content", [])
        if block.get("type") == "text"
    )

    parsed = _parse_json(text)
    if not parsed:
        fallback = _offline_pick(message)
        fallback["mode"] = "degraded"
        fallback["reason"] = "model did not return valid JSON, used keyword match"
        return fallback

    parsed["mode"] = "llm"
    parsed["raw_model_output"] = text.strip()
    return parsed


def _parse_json(text: str) -> dict | None:
    """Pull a JSON object out of the model's reply, tolerating stray text."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?|```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
    return None


def validate_proposal(proposal: dict) -> tuple[dict | None, str]:
    """
    Check the AI's answer before anything else looks at it.

    This runs whether or not the AI behaved. It catches a model that invents a
    product code, returns a silly quantity, or tries to sneak in a price field.

    Returns (clean_proposal, error_message). One of the two is always None.
    """
    sku = proposal.get("sku")
    if not sku or sku not in catalog.BY_SKU:
        return None, f"the assistant suggested a product that does not exist: {sku!r}"

    quantity = proposal.get("quantity", 1)
    if not isinstance(quantity, int) or not (1 <= quantity <= 10):
        return None, f"invalid quantity: {quantity!r}"

    # If the model tried to name a price, we drop it and record that it tried.
    price_attempted = any(k in proposal for k in ("price", "amount", "total", "price_paise"))

    return {
        "sku": sku,
        "quantity": quantity,
        "reason": str(proposal.get("reason", ""))[:200],
        "mode": proposal.get("mode", "unknown"),
        "model_tried_to_set_price": price_attempted,
    }, None
