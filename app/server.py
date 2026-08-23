from __future__ import annotations

import hashlib
import hmac
import html
import os
from datetime import datetime, timezone

from fastmcp import FastMCP
from fastmcp.server.auth.providers.github import GitHubProvider
from key_value.aio.stores.disk import DiskStore
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from .exchange import Exchange
from .notifier import EmailNotifier
from .risk import TicketRejected, validate_ticket
from .store import Store

BASE_URL = os.environ["PUBLIC_BASE_URL"].rstrip("/")
SETUP_TOKEN = os.environ["SETUP_TOKEN"]
REST_API_TOKEN = os.getenv("REST_API_TOKEN", "")
LIVE_ARMED = os.getenv("LIVE_TRADING", "false").lower() == "true" and os.getenv("LIVE_CONFIRMATION", "") == "I_ACCEPT_THE_25_USDC_LIVE_RISK"
ALLOCATION_FRACTION = min(0.95, max(0.20, float(os.getenv("MAX_CAPITAL_ALLOCATION_PCT", "95")) / 100))
store = Store(os.getenv("DATA_DIR", "/app/data"), os.environ["CREDENTIAL_ENCRYPTION_KEY"])
notifier = EmailNotifier()
auth = GitHubProvider(
    client_id=os.environ["GITHUB_CLIENT_ID"], client_secret=os.environ["GITHUB_CLIENT_SECRET"], base_url=BASE_URL,
    client_storage=DiskStore(directory=os.path.join(os.getenv("DATA_DIR", "/app/data"), "oauth")),
)
mcp = FastMCP("Primus Coinbase Compounding Pilot", auth=auth, instructions="Private spot pilot. Research may inspect any Coinbase-listed USDC spot product, but the server independently validates every ticket. Call preflight and reconcile before any write action.")


def exchange() -> Exchange:
    return Exchange(*store.credentials())


def response_order_id(response: dict) -> str | None:
    success = response.get("success_response") or {}
    return success.get("order_id") or response.get("order_id")


def email_event(subject: str, payload: dict, ticket_id: str | None = None) -> dict:
    try:
        result = notifier.send(subject, payload)
        store.event("EMAIL_SENT" if result.get("sent") else "EMAIL_SKIPPED", {"subject": subject, **result}, ticket_id)
        return result
    except Exception as exc:
        store.event("EMAIL_FAILED", {"subject": subject, "error": str(exc)}, ticket_id)
        return {"sent": False, "reason": str(exc)}


def run_preflight() -> dict:
    result = exchange().preflight()
    baseline = store.initialize_baseline(result["usdc_total"])
    result.update({"pilot_baseline_usdc": baseline, "permitted_capital_usdc": store.permitted_capital(), "allocation_fraction": ALLOCATION_FRACTION})
    store.event("PREFLIGHT_OK", result)
    return result


def reconcile() -> dict:
    position = store.open_position()
    if not position:
        return {"open_position": None, "realized_pnl_usdc": float(store.setting("realized_pnl_usdc", "0") or 0)}
    ex = exchange()
    order = ex.get_order(position["order_id"]) if position.get("order_id") else {}
    status = str(order.get("status", position["status"])).upper()
    filled_size = float(order.get("filled_size") or order.get("cumulative_quantity") or position["base_size"] or 0)
    fees = float(order.get("total_fees") or position["entry_fees"] or 0)
    product = ex.product(position["product_id"])
    base_balance = ex.base_balance(position["product_id"])
    mark_value = base_balance * product["price"]
    net_pnl = mark_value - float(position["entry_notional"]) - fees
    prior_status = position["status"]
    terminal = status in {"CANCELLED", "FAILED", "EXPIRED"} and base_balance <= 0
    closed = prior_status == "FILLED" and base_balance <= 0
    if closed:
        pf = ex.preflight()
        capital_before_close = store.permitted_capital()
        exit_value = pf["usdc_total"]
        realized = exit_value - capital_before_close
        store.update_position(position["ticket_id"], status="CLOSED", closed_at=datetime.now(timezone.utc).isoformat(), exit_value=exit_value, realized_pnl=realized, base_size=0, entry_fees=fees)
        store.add_realized_pnl(realized)
        store.event("POSITION_CLOSED", {"product_id": position["product_id"], "realized_pnl_usdc": realized, "permitted_capital_usdc": store.permitted_capital()}, position["ticket_id"])
        email_event("[LIVE TRADE CLOSED] Coinbase Pilot P&L", {"product": position["product_id"], "realized_pnl_usdc": round(realized, 4), "result": "GAIN" if realized >= 0 else "LOSS", "permitted_capital_usdc": round(store.permitted_capital(), 4)}, position["ticket_id"])
        return {"open_position": None, "realized_pnl_usdc": realized, "permitted_capital_usdc": store.permitted_capital()}
    if terminal:
        store.update_position(position["ticket_id"], status=status)
        store.event("ORDER_TERMINAL_UNFILLED", {"status": status}, position["ticket_id"])
    else:
        store.update_position(position["ticket_id"], status="FILLED" if filled_size > 0 else status, base_size=filled_size, entry_fees=fees)
        if prior_status != "FILLED" and filled_size > 0:
            email_event("[LIVE TRADE FILLED] Coinbase Pilot", {"product": position["product_id"], "base_size": filled_size, "entry_notional_usdc": position["entry_notional"], "fees_usdc": fees}, position["ticket_id"])
    return {"open_position": {**position, "status": "FILLED" if filled_size > 0 else status, "base_balance": base_balance, "mark_price": product["price"], "mark_value_usdc": mark_value, "net_unrealized_pnl_usdc": net_pnl}, "permitted_capital_usdc": store.permitted_capital()}


def get_pilot_status() -> dict:
    reconciliation = reconcile() if store.setting("pilot_baseline_usdc") else {"open_position": store.open_position()}
    return {"mode": "LIVE_ARMED" if LIVE_ARMED else "DRY_RUN_LOCKED", "paused": store.paused(), "baseline_usdc": float(store.setting("pilot_baseline_usdc", "0") or 0), "permitted_capital_usdc": store.permitted_capital(), "reconciliation": reconciliation, "email_configured": notifier.configured, "events": store.recent()}


def discover_products() -> dict:
    products = exchange().eligible_products()
    return {"count": len(products), "products": products, "rule": "Coinbase-listed, tradable USDC spot products only; research and ticket gates still apply"}


def run_validated_ticket(ticket: dict) -> dict:
    ticket_id = str(ticket.get("ticket_id", ""))
    if not ticket_id:
        raise ValueError("ticket_id required")
    if store.paused():
        raise RuntimeError("live pilot is paused")
    if store.seen(ticket_id):
        raise RuntimeError("duplicate ticket rejected")
    reconciliation = reconcile()
    if reconciliation.get("open_position"):
        raise RuntimeError("one-position limit reached")
    ex = exchange()
    pf = run_preflight()
    product = ex.product(str(ticket.get("product_id", "")))
    try:
        validate_ticket(ticket, available_usdc=pf["usdc_available"], permitted_capital=store.permitted_capital(), open_positions=0, product=product, allocation_fraction=ALLOCATION_FRACTION)
    except TicketRejected as exc:
        store.event("TICKET_REJECTED", {"reason": str(exc)}, ticket_id)
        raise
    store.event("TICKET_ACCEPTED", {**ticket, "verified_product": product, "permitted_capital_usdc": store.permitted_capital()}, ticket_id)
    preview = ex.preview_buy(ticket)
    store.event("ORDER_PREVIEWED", {"preview_sha256": hashlib.sha256(str(preview).encode()).hexdigest()}, ticket_id)
    if not LIVE_ARMED:
        return {"status": "DRY_RUN_ONLY", "ticket_id": ticket_id, "message": "No real order submitted", "permitted_capital_usdc": store.permitted_capital()}
    response = ex.submit_buy(ticket)
    order_id = response_order_id(response)
    if not order_id:
        raise RuntimeError("Coinbase did not return an order id; submission not recorded as open")
    store.add_submitted_position(ticket, order_id)
    store.event("ORDER_SUBMITTED", {"order_id": order_id, "product_id": ticket["product_id"], "notional_usdc": ticket["notional_usdc"]}, ticket_id)
    email_event("[LIVE ORDER SUBMITTED] Coinbase Pilot", {"ticket_id": ticket_id, "order_id": order_id, "product": ticket["product_id"], "notional_usdc": ticket["notional_usdc"], "limit": ticket["limit_price"], "stop": ticket["stop_price"], "target": ticket["target_price"]}, ticket_id)
    return {"status": "ORDER_SUBMITTED", "ticket_id": ticket_id, "order_id": order_id}


def pause_live_trading(reason: str) -> dict:
    store.event("PAUSED", {"reason": reason})
    email_event("[LIVE PILOT PAUSED]", {"reason": reason})
    return {"paused": True, "reason": reason}


def rest_authorized(request: Request) -> bool:
    if not REST_API_TOKEN:
        return False
    supplied = request.headers.get("authorization", "")
    if supplied.lower().startswith("bearer "):
        supplied = supplied[7:]
    return hmac.compare_digest(supplied, REST_API_TOKEN)


def unauthorized() -> JSONResponse:
    return JSONResponse({"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request):
    return JSONResponse({"ok": True, "mode": "LIVE_ARMED" if LIVE_ARMED else "DRY_RUN_LOCKED"})


@mcp.custom_route("/setup", methods=["GET", "POST"])
async def setup(request: Request):
    if not hmac.compare_digest(request.query_params.get("token", ""), SETUP_TOKEN):
        return HTMLResponse("Not found", status_code=404)
    message = ""
    if request.method == "POST":
        form = await request.form()
        key, secret = str(form.get("key_name", "")).strip(), str(form.get("private_key", "")).strip()
        if not key or "PRIVATE KEY" not in secret:
            message = "Invalid key fields. Nothing saved."
        else:
            store.save_credentials(key, secret)
            message = "Credentials encrypted and saved. Rotate SETUP_TOKEN now."
    page = f'''<!doctype html><meta name="viewport" content="width=device-width"><title>Coinbase pilot setup</title><style>body{{font:16px system-ui;background:#081226;color:#edf3ff;margin:0}}main{{max-width:620px;margin:30px auto;padding:24px;background:#111d35;border-radius:18px}}input,textarea,button{{box-sizing:border-box;width:100%;margin:8px 0 18px;padding:13px;border-radius:9px;border:1px solid #536583}}textarea{{height:220px}}button{{background:#276ef1;color:white;font-weight:700}}.warn{{color:#ffcf70}}</style><main><h1>Coinbase compounding pilot</h1><p class="warn">Portfolio-scoped CDP key: View + Trade only; Transfer disabled. Values go directly to this server.</p><p>{html.escape(message)}</p><form method="post"><label>API key name</label><input name="key_name" required><label>Private key</label><textarea name="private_key" required></textarea><button>Encrypt and save</button></form></main>'''
    return HTMLResponse(page)


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def pilot_status() -> dict:
    """Reconcile Coinbase state and return capital, live position, P&L, email, and audit status."""
    return get_pilot_status()


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def preflight_coinbase() -> dict:
    """Verify portfolio-scoped permissions, initialize the immutable pilot baseline, and report compounding capital."""
    return run_preflight()


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def list_eligible_spot_products() -> dict:
    """List current tradable Coinbase USDC spot products; this is discovery, not a recommendation."""
    return discover_products()


@mcp.tool(annotations={"destructiveHint": True, "openWorldHint": True, "idempotentHint": True})
def execute_validated_ticket(ticket: dict) -> dict:
    """Preview and, only when armed, submit one expiring, server-validated Coinbase spot ticket with stop and target."""
    return run_validated_ticket(ticket)


@mcp.tool(annotations={"destructiveHint": True, "idempotentHint": True})
def emergency_pause(reason: str) -> dict:
    """Block new submissions. Existing Coinbase bracket orders remain on exchange."""
    return pause_live_trading(reason)


@mcp.custom_route("/api/pilot-status", methods=["GET"])
async def rest_pilot_status(request: Request):
    if not rest_authorized(request): return unauthorized()
    try: return JSONResponse(get_pilot_status())
    except Exception as exc: return JSONResponse({"error": str(exc)}, status_code=500)


@mcp.custom_route("/api/preflight", methods=["GET"])
async def rest_preflight(request: Request):
    if not rest_authorized(request): return unauthorized()
    try: return JSONResponse(run_preflight())
    except Exception as exc: return JSONResponse({"error": str(exc)}, status_code=500)


@mcp.custom_route("/api/execute-ticket", methods=["POST"])
async def rest_execute_ticket(request: Request):
    if not rest_authorized(request): return unauthorized()
    try: return JSONResponse(run_validated_ticket((await request.json()).get("ticket", {})))
    except ValueError as exc: return JSONResponse({"error": str(exc)}, status_code=400)
    except RuntimeError as exc: return JSONResponse({"error": str(exc)}, status_code=409)
    except Exception as exc: return JSONResponse({"error": str(exc)}, status_code=500)


@mcp.custom_route("/api/emergency-pause", methods=["POST"])
async def rest_emergency_pause(request: Request):
    if not rest_authorized(request): return unauthorized()
    try: return JSONResponse(pause_live_trading((await request.json()).get("reason", "Manual emergency pause")))
    except Exception as exc: return JSONResponse({"error": str(exc)}, status_code=500)


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
