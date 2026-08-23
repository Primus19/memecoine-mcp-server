from __future__ import annotations

import html
import os

from fastmcp import FastMCP
from fastmcp.server.auth.providers.github import GitHubProvider
from key_value.aio.stores.disk import DiskStore
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from .exchange import Exchange
from .risk import TicketRejected, validate_ticket
from .store import Store

BASE_URL = os.environ["PUBLIC_BASE_URL"].rstrip("/")
SETUP_TOKEN = os.environ["SETUP_TOKEN"]
ALLOWLIST = {x.strip().upper() for x in os.getenv("PRODUCT_ALLOWLIST", "DOGE-USDC,BONK-USDC,SHIB-USDC,PEPE-USDC,WIF-USDC,TURBO-USDC").split(",") if x.strip()}
LIVE_ARMED = os.getenv("LIVE_TRADING", "false").lower() == "true" and os.getenv("LIVE_CONFIRMATION", "") == "I_ACCEPT_THE_25_USDC_LIVE_RISK"
store = Store(os.getenv("DATA_DIR", "/app/data"), os.environ["CREDENTIAL_ENCRYPTION_KEY"])
auth = GitHubProvider(
    client_id=os.environ["GITHUB_CLIENT_ID"],
    client_secret=os.environ["GITHUB_CLIENT_SECRET"],
    base_url=BASE_URL,
    client_storage=DiskStore(directory=os.path.join(os.getenv("DATA_DIR", "/app/data"), "oauth")),
)
mcp = FastMCP("Primus Coinbase $25 Pilot", auth=auth, instructions="Private $25 spot pilot. Never invent tickets or credentials. Call preflight before submit. Write tools are high-impact and server-validated.")


def exchange() -> Exchange:
    return Exchange(*store.credentials())


def get_pilot_status() -> dict:
    """Plain service implementation shared by MCP and REST transports."""
    return {
        "mode": "LIVE_ARMED" if LIVE_ARMED else "DRY_RUN_LOCKED",
        "paused": store.paused(),
        "open_position": store.open_position(),
        "allowlist": sorted(ALLOWLIST),
        "events": store.recent(),
    }


def run_preflight() -> dict:
    """Plain service implementation shared by MCP and REST transports."""
    result = exchange().preflight()
    store.event("PREFLIGHT_OK", result)
    return result


def run_validated_ticket(ticket: dict) -> dict:
    """Plain service implementation shared by MCP and REST transports."""
    ticket_id = str(ticket.get("ticket_id", ""))
    if not ticket_id:
        raise ValueError("ticket_id required")
    if store.paused():
        raise RuntimeError("live pilot is paused")
    if store.seen(ticket_id):
        raise RuntimeError("duplicate ticket rejected")
    ex = exchange()
    pf = ex.preflight()
    try:
        validate_ticket(ticket, pf["usdc_equity"], int(store.open_position()), ALLOWLIST)
    except TicketRejected as exc:
        store.event("TICKET_REJECTED", {"reason": str(exc)}, ticket_id)
        raise
    store.event("TICKET_ACCEPTED", ticket, ticket_id)
    preview = ex.preview_buy(ticket)
    store.event("ORDER_PREVIEWED", {"preview": str(preview)}, ticket_id)
    if not LIVE_ARMED:
        return {"status": "DRY_RUN_ONLY", "ticket_id": ticket_id, "message": "No real order submitted"}
    result = ex.submit_buy(ticket)
    store.event("ORDER_SUBMITTED", {"response": str(result)}, ticket_id)
    return {"status": "ORDER_SUBMITTED", "ticket_id": ticket_id, "response": str(result)}


def pause_live_trading(reason: str) -> dict:
    """Plain service implementation shared by MCP and REST transports."""
    store.event("PAUSED", {"reason": reason})
    return {"paused": True, "reason": reason}


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request):
    return JSONResponse({"ok": True, "mode": "LIVE_ARMED" if LIVE_ARMED else "DRY_RUN_LOCKED"})


@mcp.custom_route("/setup", methods=["GET", "POST"])
async def setup(request: Request):
    token = request.query_params.get("token", "")
    if token != SETUP_TOKEN:
        return HTMLResponse("Not found", status_code=404)
    message = ""
    if request.method == "POST":
        form = await request.form()
        key = str(form.get("key_name", "")).strip()
        secret = str(form.get("private_key", "")).strip()
        if not key or "PRIVATE KEY" not in secret:
            message = "Invalid key fields. Nothing saved."
        else:
            store.save_credentials(key, secret)
            message = "Credentials encrypted and saved. Delete or rotate SETUP_TOKEN now."
    page = f'''<!doctype html><meta name="viewport" content="width=device-width"><title>Coinbase pilot setup</title><style>body{{font:16px system-ui;background:#081226;color:#edf3ff;margin:0}}main{{max-width:620px;margin:30px auto;padding:24px;background:#111d35;border-radius:18px}}input,textarea,button{{box-sizing:border-box;width:100%;margin:8px 0 18px;padding:13px;border-radius:9px;border:1px solid #536583}}textarea{{height:220px}}button{{background:#276ef1;color:white;font-weight:700}}.warn{{color:#ffcf70}}</style><main><h1>Coinbase $25 Pilot</h1><p class="warn">Enter a portfolio-scoped CDP key with View + Trade only. Transfer must be disabled. These values go directly to this server and are never sent to ChatGPT.</p><p>{html.escape(message)}</p><form method="post"><label>API key name</label><input name="key_name" autocomplete="off" required><label>EC private key</label><textarea name="private_key" autocomplete="off" required></textarea><button>Encrypt and save credentials</button></form></main>'''
    return HTMLResponse(page)


@mcp.tool(annotations={"readOnlyHint": True})
def pilot_status() -> dict:
    """Return live-pilot mode, pause/position state, allowlist, and redacted audit history."""
    return get_pilot_status()


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def preflight_coinbase() -> dict:
    """Verify Coinbase key permissions and dedicated USDC balance. Never returns credentials."""
    return run_preflight()


@mcp.tool(annotations={"destructiveHint": True, "openWorldHint": True, "idempotentHint": True})
def execute_validated_ticket(ticket: dict) -> dict:
    """Preview and, only when armed, submit one expiring Coinbase spot ticket with attached stop/target. This can spend real money."""
    return run_validated_ticket(ticket)


@mcp.tool(annotations={"destructiveHint": True, "idempotentHint": True})
def emergency_pause(reason: str) -> dict:
    """Immediately block new live submissions. Existing Coinbase bracket orders remain on exchange."""
    return pause_live_trading(reason)


@mcp.custom_route("/api/pilot-status", methods=["GET"])
async def rest_pilot_status(_: Request):
    try:
        return JSONResponse(get_pilot_status())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@mcp.custom_route("/api/preflight", methods=["GET"])
async def rest_preflight(_: Request):
    try:
        return JSONResponse(run_preflight())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@mcp.custom_route("/api/execute-ticket", methods=["POST"])
async def rest_execute_ticket(request: Request):
    try:
        body = await request.json()
        return JSONResponse(run_validated_ticket(body.get("ticket", {})))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@mcp.custom_route("/api/emergency-pause", methods=["POST"])
async def rest_emergency_pause(request: Request):
    try:
        body = await request.json()
        return JSONResponse(pause_live_trading(body.get("reason", "Manual emergency pause")))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
