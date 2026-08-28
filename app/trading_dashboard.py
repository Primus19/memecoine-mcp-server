from __future__ import annotations

import base64
import hmac
import html
import json
import os
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _money(value: Any) -> str:
    number = _number(value)
    sign = "+" if number > 0 else ""
    return f"{sign}${number:,.4f}"


def _color(value: Any) -> str:
    number = _number(value)
    return "#15803d" if number > 0 else "#b91c1c" if number < 0 else "#334155"


def _escape(value: Any) -> str:
    return html.escape(str(value if value not in (None, "") else "N/A"))


def _urls(name: str, defaults: tuple[str, ...]) -> list[str]:
    configured = [item.strip() for item in os.getenv(name, "").split(",") if item.strip()]
    return configured or list(defaults)


def fetch_first(urls: list[str], timeout: int = 8) -> tuple[dict, str, str]:
    errors = []
    for url in urls:
        try:
            request = urllib.request.Request(url, headers={
                "Accept": "application/json",
                "User-Agent": "primus-trading-dashboard/1.0",
            })
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if isinstance(payload, dict):
                return payload, url, ""
            errors.append(f"{url}: response was not an object")
        except (OSError, ValueError, urllib.error.URLError) as exc:
            errors.append(f"{url}: {type(exc).__name__}")
    return {}, "", "; ".join(errors[-3:]) or "no upstream URL configured"


def dashboard_authorized(authorization: str) -> bool:
    password = os.getenv("DASHBOARD_PASSWORD", "") or os.getenv("REST_API_TOKEN", "")
    username = os.getenv("DASHBOARD_USERNAME", "primus")
    if not password or not authorization.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(authorization.split(None, 1)[1]).decode("utf-8")
        supplied_user, supplied_password = decoded.split(":", 1)
    except (ValueError, UnicodeDecodeError, base64.binascii.Error):
        return False
    return hmac.compare_digest(supplied_user, username) and hmac.compare_digest(supplied_password, password)


def build_snapshot(coinbase_status: Callable[[], dict]) -> dict:
    generated = datetime.now(timezone.utc).isoformat()
    sources: dict[str, dict] = {}
    try:
        coinbase = coinbase_status()
        sources["coinbase"] = {"ok": True, "source": "local authoritative ledger", "error": ""}
    except Exception as exc:  # dashboard failure must not affect trading
        coinbase = {}
        sources["coinbase"] = {"ok": False, "source": "", "error": f"{type(exc).__name__}: {str(exc)[:160]}"}

    forex_urls = _urls("DASHBOARD_FOREX_URLS", (
        "http://forex-executor-live.railway.internal:8080/report.json",
        "http://forex-executor.railway.internal:8080/report.json",
    ))
    solana_urls = _urls("DASHBOARD_SOLANA_EXECUTOR_URLS", (
        "http://solana-early-executor.railway.internal:8080/health",
        "https://solana-early-executor-production.up.railway.app/health",
    ))
    discovery_urls = _urls("DASHBOARD_SOLANA_DISCOVERY_URLS", (
        "http://solana-early-discovery.railway.internal:8080/status",
        "https://solana-early-discovery-production.up.railway.app/status",
    ))
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="dashboard-read") as pool:
        forex_future = pool.submit(fetch_first, forex_urls, 8)
        solana_future = pool.submit(fetch_first, solana_urls, 8)
        discovery_future = pool.submit(fetch_first, discovery_urls, 8)
        forex, forex_url, forex_error = forex_future.result()
        solana, solana_url, solana_error = solana_future.result()
        discovery, discovery_url, discovery_error = discovery_future.result()
    sources["forex"] = {"ok": bool(forex), "source": forex_url, "error": forex_error}
    sources["solana"] = {"ok": bool(solana), "source": solana_url, "error": solana_error}
    sources["discovery"] = {"ok": bool(discovery), "source": discovery_url, "error": discovery_error}
    return {"generated_at": generated, "coinbase": coinbase, "forex": forex,
            "solana": solana, "discovery": discovery, "sources": sources}


def _status_badge(label: str, good: bool) -> str:
    background = "#dcfce7" if good else "#fee2e2"
    foreground = "#166534" if good else "#991b1b"
    return f'<span style="display:inline-block;padding:5px 9px;border-radius:999px;background:{background};color:{foreground};font-size:11px;font-weight:800">{_escape(label)}</span>'


def _position_rows(snapshot: dict) -> str:
    rows = []
    position = ((snapshot.get("coinbase") or {}).get("portfolio") or {}).get("open_position")
    if position:
        rows.append(("Coinbase", position.get("product_id"), position.get("status"),
                     position.get("net_qty") or (position.get("fills") or {}).get("net_qty"),
                     position.get("entry_price"), position.get("mark_price"),
                     position.get("net_unrealized_pnl_usdc")))
    forex = snapshot.get("forex") or {}
    broker = forex.get("broker") or forex.get("reconciliation") or {}
    positions = forex.get("open_positions") or broker.get("open_trades") or broker.get("positions") or []
    if isinstance(positions, dict):
        positions = list(positions.values())
    for item in positions:
        rows.append(("OANDA", item.get("symbol") or item.get("pair") or item.get("instrument"),
                     item.get("side") or ("BUY" if _number(item.get("units")) > 0 else "SELL"),
                     item.get("units") or item.get("quantity"), item.get("entry_price") or item.get("price"),
                     item.get("mark_price") or item.get("current_price"),
                     item.get("unrealized_pl") or item.get("unrealized_pnl_usd")))
    for item in (snapshot.get("solana") or {}).get("paperPositions") or []:
        rows.append(("Solana paper", item.get("symbol") or item.get("mint"), "PAPER",
                     item.get("quantity"), item.get("entryPrice") or item.get("entry_price"),
                     item.get("markPrice") or item.get("mark_price"), item.get("unrealizedPnlUsd")))
    if not rows:
        return '<tr><td colspan="7" style="padding:15px;color:#64748b">No open positions reported.</td></tr>'
    return "".join(
        f'<tr><td>{_escape(system)}</td><td><b>{_escape(asset)}</b></td><td>{_escape(side)}</td>'
        f'<td>{_escape(qty)}</td><td>{_escape(entry)}</td><td>{_escape(mark)}</td>'
        f'<td style="font-weight:800;color:{_color(pnl)}">{_money(pnl)} <small>NOT FINAL</small></td></tr>'
        for system, asset, side, qty, entry, mark, pnl in rows
    )


def _candidate_rows(discovery: dict) -> str:
    candidates = discovery.get("candidates") or []
    candidates = sorted(candidates, key=lambda item: _number(item.get("score")), reverse=True)[:12]
    if not candidates:
        return '<tr><td colspan="7" style="padding:15px;color:#64748b">No qualifying candidates in the latest scan.</td></tr>'
    rows = []
    for item in candidates:
        reasons = item.get("rejection_reasons") or item.get("rejections") or item.get("blockers") or []
        if isinstance(reasons, str):
            reasons = [reasons]
        rows.append(f'<tr><td><b>{_escape(item.get("symbol") or item.get("name") or item.get("mint"))}</b></td>'
                    f'<td>{_escape(item.get("age_minutes") or item.get("token_age_minutes"))}</td>'
                    f'<td>{_escape(item.get("score"))}</td><td>{_escape(item.get("liquidity_usd"))}</td>'
                    f'<td>{_escape(item.get("market_cap_usd"))}</td><td>{_escape(item.get("net_buy_pressure"))}</td>'
                    f'<td>{_escape(", ".join(map(str, reasons[:3])) if reasons else "eligible/observing")}</td></tr>')
    return "".join(rows)


def render_dashboard(snapshot: dict) -> str:
    coinbase = snapshot.get("coinbase") or {}
    forex = snapshot.get("forex") or {}
    solana = snapshot.get("solana") or {}
    controls = ((coinbase.get("portfolio") or {}).get("controls") or {})
    cb_pnl = coinbase.get("realized_pnl_usdc")
    cb_value = controls.get("equity_usdc") or coinbase.get("permitted_capital_usdc")
    broker = forex.get("broker") or forex.get("account") or forex.get("reconciliation") or {}
    fx_pnl = broker.get("realized_pl") if broker.get("realized_pl") is not None else forex.get("realized_pnl_usd")
    fx_nav = broker.get("NAV") or broker.get("nav") or forex.get("nav")
    sol_pnl = solana.get("paperRealizedPnlUsd")
    sources = snapshot.get("sources") or {}
    source_badges = " ".join(_status_badge(name.upper(), bool(data.get("ok"))) for name, data in sources.items())
    errors = [f"{name}: {data.get('error')}" for name, data in sources.items() if data.get("error")]
    service_errors = list(solana.get("errors") or [])[-3:]
    error_items = "".join(f"<li>{_escape(item)}</li>" for item in (errors + service_errors)) or "<li>No current dashboard-source errors.</li>"
    candidates = (snapshot.get("discovery") or {}).get("candidates") or []
    qualified = sum(1 for item in candidates if item.get("qualified") or item.get("eligible"))
    balance = solana.get("balances") or {}
    refresh = max(15, min(300, int(os.getenv("DASHBOARD_REFRESH_SECONDS", "30"))))
    return f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="{refresh}"><title>Primus Trading Command Center</title></head>
<body style="margin:0;background:#eef3f8;color:#172033;font-family:Inter,Arial,sans-serif"><div style="max-width:1220px;margin:auto;padding:22px">
<div style="background:linear-gradient(135deg,#0f172a,#183b66);border-radius:18px;padding:25px;color:white"><div style="font-size:12px;letter-spacing:1.5px;color:#93c5fd;font-weight:800">PRIMUS TRADING COMMAND CENTER</div><div style="display:flex;gap:18px;justify-content:space-between;align-items:end;flex-wrap:wrap"><div><h1 style="margin:8px 0 4px;font-size:30px">Live portfolio and market intelligence</h1><div style="color:#cbd5e1">Read-only dashboard · refreshes every {refresh} seconds · {_escape(snapshot.get("generated_at"))}</div></div><div>{source_badges}</div></div></div>
<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:13px;margin:16px 0">
<div class="card" style="background:white;border-radius:13px;padding:18px;border:1px solid #dbe3ed"><div style="color:#64748b;font-size:12px">COINBASE VALUE</div><div style="font-size:25px;font-weight:800;margin:6px 0">{_money(cb_value)}</div><div style="color:{_color(cb_pnl)};font-weight:800">{_money(cb_pnl)} cumulative realized</div><div style="margin-top:7px">{_status_badge(coinbase.get("mode") or "UNAVAILABLE", bool(coinbase))}</div></div>
<div class="card" style="background:white;border-radius:13px;padding:18px;border:1px solid #dbe3ed"><div style="color:#64748b;font-size:12px">OANDA NAV</div><div style="font-size:25px;font-weight:800;margin:6px 0">{_money(fx_nav)}</div><div style="color:{_color(fx_pnl)};font-weight:800">{_money(fx_pnl)} cumulative realized</div><div style="margin-top:7px">{_status_badge(forex.get("mode") or "UNAVAILABLE", bool(forex))}</div></div>
<div class="card" style="background:white;border-radius:13px;padding:18px;border:1px solid #dbe3ed"><div style="color:#64748b;font-size:12px">SOLANA PAPER RESULT</div><div style="font-size:25px;font-weight:800;margin:6px 0;color:{_color(sol_pnl)}">{_money(sol_pnl)}</div><div>{int(solana.get("paperObservations") or 0)} observations · {len(solana.get("paperPositions") or [])} open</div><div style="margin-top:7px">{_status_badge("PAPER / SHADOW", bool(solana))}</div></div>
<div class="card" style="background:white;border-radius:13px;padding:18px;border:1px solid #dbe3ed"><div style="color:#64748b;font-size:12px">SOLANA WALLET</div><div style="font-size:22px;font-weight:800;margin:6px 0">{_escape(balance.get("usdc"))} USDC</div><div>{_escape(balance.get("sol"))} SOL</div><div style="margin-top:7px">{_status_badge("LIVE DISABLED" if not solana.get("live") else "LIVE ARMED", bool(solana) and not solana.get("balanceError"))}</div></div>
</div>
<section style="background:white;border:1px solid #dbe3ed;border-radius:13px;padding:18px;margin:14px 0;overflow:auto"><h2 style="margin:0 0 12px">Open positions <span style="font-size:12px;color:#b45309">UNREALIZED · NOT FINAL</span></h2><table style="width:100%;border-collapse:collapse;font-size:13px"><tr style="background:#f1f5f9"><th>System</th><th>Asset</th><th>Side/status</th><th>Quantity</th><th>Entry</th><th>Mark</th><th>Unrealized P&amp;L</th></tr>{_position_rows(snapshot)}</table></section>
<section style="background:white;border:1px solid #dbe3ed;border-radius:13px;padding:18px;margin:14px 0;overflow:auto"><div style="display:flex;justify-content:space-between;gap:12px;align-items:center"><h2 style="margin:0 0 12px">Solana market candidates</h2><div>{len(candidates)} scanned candidates · {qualified} explicitly qualified</div></div><table style="width:100%;border-collapse:collapse;font-size:13px"><tr style="background:#f1f5f9"><th>Token</th><th>Age min</th><th>Score</th><th>Liquidity $</th><th>Market cap $</th><th>Buy pressure</th><th>Decision / blockers</th></tr>{_candidate_rows(snapshot.get("discovery") or {})}</table></section>
<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:14px"><section style="background:white;border:1px solid #dbe3ed;border-radius:13px;padding:18px"><h2 style="margin-top:0">Risk posture</h2><p><b>Coinbase:</b> {_escape(controls.get("recovery_mode"))} · {_escape(controls.get("recommended_allocation_fraction"))} allocation · {_escape(controls.get("drawdown_pct"))}% drawdown</p><p><b>Solana:</b> ${_escape((solana.get("limits") or {}).get("live", {}).get("entryUsd"))} max live entry · live={_escape(solana.get("live"))} · ready={_escape(solana.get("ready"))}</p><p><b>Paper evidence:</b> {_escape((solana.get("explorationPaperStats") or {}).get("closed"))} closes · {_money((solana.get("explorationPaperStats") or {}).get("expectancyUsd"))} expectancy</p></section><section style="background:white;border:1px solid #dbe3ed;border-radius:13px;padding:18px"><h2 style="margin-top:0">Service warnings</h2><ul style="padding-left:18px;line-height:1.55">{error_items}</ul></section></div>
<div style="padding:16px 3px;color:#64748b;font-size:12px;line-height:1.5">This page performs GET-only reads. It cannot issue, modify, or close trades. Coinbase/OANDA figures represent broker-confirmed production state when their source badge is green. Solana paper results are simulated and never included as realized cash profit.</div>
</div><style>th,td{{padding:9px;text-align:left;border-bottom:1px solid #e5e7eb;white-space:nowrap}}small{{font-size:9px;color:#b45309}}@media(max-width:650px){{h1{{font-size:23px!important}}th,td{{font-size:11px;padding:7px}}}}</style></body></html>'''
