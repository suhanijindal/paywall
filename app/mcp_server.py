from __future__ import annotations
"""
An MCP server that lets an outside AI assistant buy from this merchant.

WHAT MCP IS
-----------
The Model Context Protocol (MCP) is a standard way for AI assistants - Claude
Desktop, and increasingly others - to use external tools. Instead of the human
copying data in and out of the chat, the assistant is handed a list of tools it
can call directly: "search these products", "place this order". The assistant
decides when to call them and reads the results. It is USB-C for AI tools: one
protocol, and any compliant assistant can plug in.

The wire format is JSON-RPC 2.0 over stdio - the assistant launches this
process and talks to it by writing newline-delimited JSON to our stdin and
reading our replies from stdout. That is why this file must never print
anything to stdout except protocol messages; stdout IS the channel. Any logging
goes to stderr.

We implement the protocol by hand with the standard library. The official `mcp`
package needs Python 3.10+, and this project runs on 3.9, so we do not use it.

WHY A PAYMENTS COMPANY CARES
----------------------------
Razorpay ships its own MCP server so that an AI assistant can create payment
links, issue refunds, and check settlements as tools. Agentic commerce is the
whole premise of this track: the buyer is a model, not a person clicking. If
the merchant is not reachable as a set of tools, the agent cannot find it. This
server is the merchant's side of that handshake - it makes the shop sellable to
an AI buyer that speaks MCP.

WHY THERE IS NO PRICE FIELD
---------------------------
The `purchase` tool takes a SKU, a quantity, and a spending limit
(max_price_paise). It does NOT take a price, an amount, or a total, and the
tool schema advertised to the assistant has no such field. This is the entire
safety argument, expressed in the shape of the tool: a model that has been
fooled by a poisoned product description (see TEA-999 in catalog.py) can pick
the wrong item, but it has nowhere to write "and charge one rupee for it". The
price is always looked up from the catalog on our side. If a caller sends a
price field anyway, we ignore it and log `mcp.price_attempt_ignored` so the
attempt is visible in the audit trail.

Run it with:   python3 -m app.mcp_server
"""

import asyncio
import json
import sys
import uuid

from app import catalog, main, store

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "paywall"
SERVER_VERSION = "0.1.0"

# Fields a caller might use to try to name a price. None of them belong in a
# purchase request; if any appear, they are dropped and the attempt is logged.
PRICE_FIELDS = ("price", "amount", "total", "price_paise", "cost")


def _log(message: str) -> None:
    """Diagnostics go to stderr. stdout is reserved for JSON-RPC responses."""
    print("[mcp] " + message, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Tool definitions - the schema the assistant reads to know how to call us
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "search_products",
        "description": "Search the merchant's catalog by plain-English query. "
                       "Optionally filter to products at or under a price ceiling.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "What to look for, e.g. 'masala chai'"},
                "max_price_paise": {"type": "integer",
                                    "description": "Optional. Only show products "
                                                   "at or under this price, in paise."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_product",
        "description": "Get the full details of one product by its SKU, "
                       "including its description.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sku": {"type": "string", "description": "The product's SKU code"},
            },
            "required": ["sku"],
        },
    },
    {
        "name": "purchase",
        "description": "Buy a product. You must give the SKU, the quantity, and "
                       "max_price_paise (your spending limit, in paise). You "
                       "cannot set the price - the merchant looks it up. The "
                       "order is refused if the total exceeds your limit or "
                       "stock is short.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sku": {"type": "string", "description": "The product's SKU code"},
                "quantity": {"type": "integer",
                             "description": "How many units, 1 to 10"},
                "max_price_paise": {"type": "integer",
                                    "description": "REQUIRED. The most you may "
                                                   "spend on the whole order, in paise."},
            },
            # No price field exists here on purpose. See the module docstring.
            "required": ["sku", "quantity", "max_price_paise"],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _text_result(text: str, is_error: bool = False) -> dict:
    """Wrap a plain-English string as an MCP tool result."""
    result = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


def _describe(item: dict) -> str:
    rupees = item["price_paise"] / 100
    return (
        "{sku} | {name} | Rs {rupees:.2f} ({paise} paise) | stock {stock}\n"
        "  {desc}"
    ).format(
        sku=item["sku"], name=item["name"], rupees=rupees,
        paise=item["price_paise"], stock=item["stock"],
        desc=item.get("description", ""),
    )


def _tool_search_products(args: dict) -> dict:
    query = args.get("query", "")
    max_price = args.get("max_price_paise")
    matches = catalog.search(query, max_price_paise=max_price)
    if not matches:
        return _text_result("No products matched '{}'.".format(query))
    lines = ["Found {} product(s):".format(len(matches))]
    lines.extend(_describe(item) for item in matches)
    return _text_result("\n".join(lines))


def _tool_get_product(args: dict) -> dict:
    sku = args.get("sku", "")
    item = catalog.BY_SKU.get(sku)
    if not item:
        return _text_result("No product with SKU {!r}.".format(sku), is_error=True)
    return _text_result(_describe(item))


def _tool_purchase(args: dict) -> dict:
    """
    Run the same gated purchase the HTTP API runs - no shortcut path.

    Every branch logs to the events table, so the audit trail records refusals
    as well as successful orders.
    """
    sku = args.get("sku")

    # A caller must never name a price. If one slipped in, drop it and record
    # the attempt before doing anything else.
    attempted = {k: args[k] for k in PRICE_FIELDS if k in args}
    if attempted:
        store.log_event("mcp.price_attempt_ignored", {
            "sku": sku,
            "ignored_fields": attempted,
            "note": "purchase tool has no price field; the caller's price was discarded",
        })

    max_price_paise = args.get("max_price_paise")
    if max_price_paise is None:
        store.log_event("mcp.purchase_rejected", {
            "sku": sku, "reason": "max_price_paise is required",
        })
        return _text_result(
            "Refused: you must supply max_price_paise (your spending limit). "
            "No limit means no purchase.",
            is_error=True,
        )
    if not isinstance(max_price_paise, int) or max_price_paise <= 0:
        store.log_event("mcp.purchase_rejected", {
            "sku": sku, "reason": "max_price_paise must be a positive integer",
            "value": max_price_paise,
        })
        return _text_result(
            "Refused: max_price_paise must be a positive whole number of paise.",
            is_error=True,
        )

    quantity = args.get("quantity", 1)
    if not isinstance(quantity, int) or not (1 <= quantity <= 10):
        store.log_event("mcp.purchase_rejected", {
            "sku": sku, "reason": "quantity must be between 1 and 10",
            "value": quantity,
        })
        return _text_result(
            "Refused: quantity must be a whole number between 1 and 10.",
            is_error=True,
        )

    product = catalog.BY_SKU.get(sku)
    if not product:
        store.log_event("mcp.purchase_rejected", {
            "sku": sku, "reason": "no such product",
        })
        return _text_result(
            "Refused: there is no product with SKU {!r}.".format(sku),
            is_error=True,
        )

    # Hand over to the single shared purchase core. This is the SAME code the
    # /agent/buy endpoint uses, so the limit and stock checks cannot diverge.
    idempotency_key = "mcp-" + uuid.uuid4().hex[:16]
    result = asyncio.run(
        main.create_purchase(product, quantity, max_price_paise, idempotency_key)
    )

    if result["outcome"] == "over_limit":
        return _text_result(
            "Refused: {name} x{qty} totals Rs {total:.2f}, over your limit of "
            "Rs {limit:.2f} ({total_paise} vs {limit_paise} paise).".format(
                name=product["name"], qty=quantity,
                total=result["amount_paise"] / 100, limit=max_price_paise / 100,
                total_paise=result["amount_paise"], limit_paise=max_price_paise,
            ),
            is_error=True,
        )

    if result["outcome"] == "no_stock":
        return _text_result(
            "Refused: only {stock} of {name} in stock, you asked for {qty}.".format(
                stock=product["stock"], name=product["name"], qty=quantity,
            ),
            is_error=True,
        )

    # outcome == "created"
    lines = [
        "Order created: {}".format(result["order_id"]),
        "{name} x{qty}".format(name=product["name"], qty=quantity),
        "Total: Rs {:.2f} ({} paise) - the merchant's catalog price.".format(
            result["amount_paise"] / 100, result["amount_paise"]),
        "A human approves payment at {}".format(result["checkout_url"]),
    ]
    if result.get("suggestion"):
        s = result["suggestion"]
        lines.append("You might also add: {name} (Rs {price:.2f}).".format(
            name=s["name"], price=s["price_paise"] / 100))
    return _text_result("\n".join(lines))


TOOL_HANDLERS = {
    "search_products": _tool_search_products,
    "get_product": _tool_get_product,
    "purchase": _tool_purchase,
}


# ---------------------------------------------------------------------------
# JSON-RPC method handlers
# ---------------------------------------------------------------------------

def _handle_initialize(params: dict) -> dict:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    }


def _handle_tools_list(params: dict) -> dict:
    return {"tools": TOOLS}


def _handle_tools_call(params: dict) -> dict:
    name = params.get("name")
    args = params.get("arguments") or {}
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        # Signal an unknown tool as a JSON-RPC error via the sentinel below.
        raise _RpcError(-32602, "Unknown tool: {!r}".format(name))
    return handler(args)


class _RpcError(Exception):
    """Carries a JSON-RPC error code and message back to the dispatcher."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def dispatch(request: dict):
    """
    Route one JSON-RPC request to its handler and build the response.

    Returns the response dict, or None for notifications (which by the JSON-RPC
    spec have no id and get no reply).
    """
    method = request.get("method")
    params = request.get("params") or {}
    rpc_id = request.get("id")

    # Notifications carry no id and expect no response.
    if rpc_id is None and (method or "").startswith("notifications/"):
        return None

    try:
        # Make sure our tables exist before we touch the database. Every real
        # caller - the tests and the live stdio host alike - reaches the server
        # through dispatch(), not through main_loop(), so this is the only place
        # guaranteed to run before a request is handled. init_db() is a set of
        # CREATE TABLE IF NOT EXISTS statements: cheap and safe to call every
        # time. This is what stops "no such table: orders" when the database
        # file is missing (found in live testing against Claude Desktop).
        store.init_db()

        if method == "initialize":
            result = _handle_initialize(params)
        elif method == "tools/list":
            result = _handle_tools_list(params)
        elif method == "tools/call":
            result = _handle_tools_call(params)
        else:
            raise _RpcError(-32601, "Method not found: {!r}".format(method))
    except _RpcError as exc:
        return {"jsonrpc": "2.0", "id": rpc_id,
                "error": {"code": exc.code, "message": exc.message}}
    except Exception as exc:  # never crash the server on a bad call
        _log("handler error: {}: {}".format(type(exc).__name__, exc))
        return {"jsonrpc": "2.0", "id": rpc_id,
                "error": {"code": -32603, "message": "Internal error: {}".format(exc)}}

    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _write(response: dict) -> None:
    sys.stdout.write(json.dumps(response) + "\n")
    sys.stdout.flush()


def main_loop() -> None:
    """Read newline-delimited JSON-RPC requests from stdin until EOF."""
    store.init_db()
    _log("paywall MCP server ready on stdio")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            _write({"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": "Parse error: {}".format(exc)}})
            continue
        response = dispatch(request)
        if response is not None:
            _write(response)


if __name__ == "__main__":
    main_loop()
