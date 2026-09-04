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


def fetch_first(urls: list[str], timeout: int = 8,
                bearer_token: str = "") -> tuple[dict, str, str]:
    errors = []
    for url in urls:
        try:
            headers = {
                "Accept": "application/json",
                "User-Agent": "primus-trading-dashboard/1.0",
            }
            if bearer_token:
                headers["Authorization"] = f"Bearer {bearer_token}"
            request = urllib.request.Request(url, headers=headers)
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
    multi_asset_urls = _urls("DASHBOARD_MULTI_ASSET_URLS", (
        "http://multi-asset-paper-worker.railway.internal:8080/report.json",
        "http://multi-asset-worker.railway.internal:8080/report.json",
    ))
    multi_asset_token = (os.getenv("DASHBOARD_MULTI_ASSET_TOKEN", "").strip() or
                         os.getenv("MULTI_ASSET_REPORT_TOKEN", "").strip())
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="dashboard-read") as pool:
        forex_future = pool.submit(fetch_first, forex_urls, 8)
        solana_future = pool.submit(fetch_first, solana_urls, 8)
        discovery_future = pool.submit(fetch_first, discovery_urls, 8)
        multi_asset_future = pool.submit(fetch_first, multi_asset_urls, 8, multi_asset_token)
        forex, forex_url, forex_error = forex_future.result()
        solana, solana_url, solana_error = solana_future.result()
        discovery, discovery_url, discovery_error = discovery_future.result()
        multi_asset, multi_asset_url, multi_asset_error = multi_asset_future.result()
    sources["forex"] = {"ok": bool(forex), "source": forex_url, "error": forex_error}
    sources["solana"] = {"ok": bool(solana), "source": solana_url, "error": solana_error}
    sources["discovery"] = {"ok": bool(discovery), "source": discovery_url, "error": discovery_error}
    sources["multi_week"] = {"ok": bool(multi_asset), "source": multi_asset_url,
                             "error": multi_asset_error}
    return {"generated_at": generated, "coinbase": coinbase, "forex": forex,
            "solana": solana, "discovery": discovery, "multi_asset": multi_asset,
            "sources": sources}


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
    multi_asset = snapshot.get("multi_asset") or {}
    multi_week = multi_asset.get("multi_week_crypto") or {}
    multi_positions = multi_week.get("open_positions") or multi_asset.get("open_positions") or []
    for item in multi_positions:
        if item.get("strategy") and "MULTI_WEEK_CRYPTO" not in str(item.get("strategy")):
            continue
        rows.append(("Multi-week paper", item.get("symbol") or item.get("contract"), "PAPER HOLD",
                     item.get("quantity"), item.get("entry_price"), item.get("current_mark_price"),
                     item.get("current_unrealized_pnl_usd")))
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


# Interactive command-center renderer.  The earlier renderer is intentionally
# retained above for an easy rollback while this interface is deployed.
def render_dashboard(snapshot: dict) -> str:
    initial = json.dumps(snapshot, separators=(",", ":")).replace("<", "\\u003c")
    refresh = max(10, min(120, int(os.getenv("DASHBOARD_REFRESH_SECONDS", "15"))))
    page = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Primus Trading Command Center</title>
<style>
:root{--bg:#07111f;--panel:#101d2f;--panel2:#14243a;--line:#24364d;--text:#e8f0fa;--muted:#8fa3bc;--blue:#54a6ff;--green:#31d08b;--red:#ff6577;--amber:#ffbd59;--purple:#ad8cff}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px Inter,system-ui,-apple-system,Segoe UI,sans-serif}button{font:inherit}.shell{max-width:1540px;margin:auto;padding:18px}.top{display:flex;justify-content:space-between;align-items:center;gap:18px;padding:10px 2px 18px}.brand{display:flex;align-items:center;gap:12px}.mark{width:42px;height:42px;border-radius:12px;background:linear-gradient(135deg,var(--blue),var(--purple));display:grid;place-items:center;font-weight:900;color:#07111f}.brand h1{font-size:20px;margin:0}.sub,.muted{color:var(--muted)}.livebar{display:flex;align-items:center;gap:9px;flex-wrap:wrap}.pulse{width:9px;height:9px;border-radius:50%;background:var(--green);box-shadow:0 0 0 0 rgba(49,208,139,.5);animation:pulse 1.8s infinite}@keyframes pulse{70%{box-shadow:0 0 0 8px transparent}}.pill{border:1px solid var(--line);background:var(--panel);border-radius:999px;padding:7px 10px;font-size:12px}.nav{display:flex;gap:6px;overflow:auto;border-bottom:1px solid var(--line);margin-bottom:16px}.nav button{border:0;background:transparent;color:var(--muted);padding:12px 15px;cursor:pointer;white-space:nowrap;border-bottom:2px solid transparent}.nav button.active{color:var(--text);border-color:var(--blue)}.view{display:none}.view.active{display:block}.grid{display:grid;gap:12px}.metrics{grid-template-columns:repeat(6,minmax(150px,1fr))}.two{grid-template-columns:repeat(2,minmax(0,1fr))}.three{grid-template-columns:repeat(3,minmax(0,1fr))}.card{background:linear-gradient(180deg,var(--panel2),var(--panel));border:1px solid var(--line);border-radius:14px;padding:16px;min-width:0}.card h2{font-size:15px;margin:0 0 13px}.metric .label{font-size:11px;text-transform:uppercase;letter-spacing:.8px;color:var(--muted)}.metric .value{font-size:24px;font-weight:800;margin:7px 0 4px}.good{color:var(--green)}.bad{color:var(--red)}.warn{color:var(--amber)}.info{color:var(--blue)}.section{margin-top:13px}.table-wrap{overflow:auto;max-height:440px}table{width:100%;border-collapse:collapse;font-size:12px}th{color:var(--muted);font-weight:600;text-align:left;position:sticky;top:0;background:#132238;z-index:1}th,td{padding:10px;border-bottom:1px solid var(--line);white-space:nowrap}td.wrap{white-space:normal;min-width:260px;line-height:1.45}.empty{text-align:center;color:var(--muted);padding:30px}.status{display:inline-flex;align-items:center;border-radius:999px;padding:4px 8px;font-size:10px;font-weight:800;background:#253750}.status.good{background:#123b32}.status.bad{background:#481f2b}.status.warn{background:#46371c}.source-list{display:grid;gap:8px}.source{display:flex;justify-content:space-between;gap:12px;border-bottom:1px solid var(--line);padding:9px 0}.bar{height:7px;border-radius:4px;background:#263950;overflow:hidden}.bar i{height:100%;display:block;background:linear-gradient(90deg,var(--blue),var(--purple))}.timeline{display:grid;gap:8px}.event{border-left:3px solid var(--blue);background:#132238;padding:10px 12px;border-radius:3px 9px 9px 3px}.event .time{font-size:11px;color:var(--muted)}canvas{width:100%;height:220px;background:#0d1929;border-radius:10px}.notice{padding:11px 13px;border:1px solid #59451d;background:#2b2417;border-radius:10px;color:#ffd78a}.errorbox{border-left-color:var(--red)}.toolbar{display:flex;justify-content:space-between;gap:10px;align-items:center;margin-bottom:10px}.toolbar input{background:#0d1929;color:var(--text);border:1px solid var(--line);border-radius:8px;padding:8px;width:220px}.updated.flash{color:var(--green)}@media(max-width:1150px){.metrics{grid-template-columns:repeat(3,1fr)}.three{grid-template-columns:1fr 1fr}}@media(max-width:760px){.shell{padding:12px}.top{align-items:flex-start;flex-direction:column}.metrics,.two,.three{grid-template-columns:1fr 1fr}.metric .value{font-size:20px}}@media(max-width:480px){.metrics,.two,.three{grid-template-columns:1fr}.brand h1{font-size:17px}.toolbar{align-items:flex-start;flex-direction:column}.toolbar input{width:100%}}
</style></head><body><main class="shell">
<header class="top"><div class="brand"><div class="mark">P</div><div><h1>Primus Trading Command Center</h1><div class="sub">Live, read-only market and execution intelligence</div></div></div><div class="livebar"><span class="pulse" id="pulse"></span><span class="pill" id="connection">CONNECTED</span><span class="pill updated" id="updated">Loading…</span><span class="pill">Auto refresh: __REFRESH__s</span></div></header>
<nav class="nav" id="nav"><button class="active" data-view="overview">Overview</button><button data-view="coinbase">Coinbase</button><button data-view="forex">Forex / OANDA</button><button data-view="solana">Solana</button><button data-view="multiweek">Multi-week crypto</button><button data-view="market">Market Scanner</button><button data-view="performance">Model Performance</button><button data-view="intelligence">Intelligence Ledger</button><button data-view="operations">Operations</button></nav>
<section id="overview" class="view active"><div id="metrics" class="grid metrics"></div><div class="grid two section"><article class="card"><h2>Open positions · unrealized, not final</h2><div class="table-wrap" id="positions"></div></article><article class="card"><h2>System readiness</h2><div id="readiness" class="source-list"></div></article></div><div class="grid two section"><article class="card"><h2>Recent trade activity</h2><div class="timeline" id="activity"></div></article><article class="card"><h2>What needs attention</h2><div class="timeline" id="attention"></div></article></div></section>
<section id="coinbase" class="view"><div class="grid metrics" id="cbMetrics"></div><article class="card section"><h2>Coinbase position and risk controls</h2><div class="table-wrap" id="cbState"></div></article><div class="grid two section"><article class="card"><h2>Recent authoritative events</h2><div class="timeline" id="cbEvents"></div></article><article class="card"><h2>Model review and execution evidence</h2><pre id="cbReview" class="muted" style="white-space:pre-wrap;word-break:break-word"></pre></article></div></section>
<section id="forex" class="view"><div class="grid metrics" id="fxMetrics"></div><div class="grid two section"><article class="card"><h2>Broker-confirmed open trades</h2><div class="table-wrap" id="fxPositions"></div></article><article class="card"><h2>Risk and account state</h2><div id="fxRisk" class="source-list"></div></article></div><article class="card section"><h2>Signal intents, outcomes and rejection reasons</h2><div class="table-wrap" id="fxIntents"></div></article></section>
<section id="solana" class="view"><div class="grid metrics" id="solMetrics"></div><div class="notice section">Paper results are simulated and never included in cash profit. Live wallet state is labeled separately.</div><div class="grid two section"><article class="card"><h2>Open paper positions</h2><div class="table-wrap" id="solPositions"></div></article><article class="card"><h2>Recent paper actions</h2><div class="table-wrap" id="solActions"></div></article></div><div class="grid two section"><article class="card"><h2>Strategy comparison</h2><div class="table-wrap" id="strategies"></div></article><article class="card"><h2>Wallet intelligence and discovery</h2><div id="walletIntel" class="source-list"></div></article></div></section>
<section id="multiweek" class="view"><div class="grid metrics" id="mwMetrics"></div><div class="notice section">Multi-week positions are paper-only. Values use the latest executable mark and are shown in USD; stale marks are warnings, not current prices.</div><div class="grid two section"><article class="card"><h2>Open holds and risk state</h2><div class="table-wrap" id="mwPositions"></div></article><article class="card"><h2>Monitor and persistence</h2><div id="mwMonitor" class="source-list"></div></article></div><div class="grid two section"><article class="card"><h2>Recent actions</h2><div class="table-wrap" id="mwActions"></div></article><article class="card"><h2>Emerging candidate shortlist</h2><div class="table-wrap" id="mwCandidates"></div></article></div></section>
<section id="market" class="view"><article class="card"><div class="toolbar"><div><h2 style="margin:0">Live Solana candidate scanner</h2><span class="muted" id="scanSummary"></span></div><input id="candidateFilter" placeholder="Filter token or decision"></div><div class="table-wrap"><table><thead><tr><th>Token</th><th>Strategy</th><th>Score</th><th>Age</th><th>Liquidity</th><th>5m buyers</th><th>Buy pressure</th><th>Wallets</th><th>Decision / blockers</th></tr></thead><tbody id="candidateRows"></tbody></table></div></article><div class="grid two section"><article class="card"><h2>Scanner diagnostics</h2><div id="scanDiagnostics" class="source-list"></div></article><article class="card"><h2>Data freshness and provenance</h2><div id="provenance" class="source-list"></div></article></div></section>
<section id="performance" class="view"><div class="grid two"><article class="card"><h2>Strategy net P&amp;L comparison</h2><canvas id="pnlChart" width="700" height="250"></canvas></article><article class="card"><h2>Win rate and expectancy</h2><div class="table-wrap" id="performanceTable"></div></article></div><div class="grid two section"><article class="card"><h2>Post-exit counterfactuals</h2><div class="table-wrap" id="counterfactuals"></div></article><article class="card"><h2>Model-improvement evidence</h2><div id="modelEvidence" class="timeline"></div></article></div></section>
<section id="intelligence" class="view"><div class="grid metrics" id="intelMetrics"></div><div class="notice section">Append-only evidence. Recommendations are ranked, testable, and advisory: they cannot modify live rules or promote a strategy automatically.</div><article class="card section"><h2>Prioritized actionable recommendations</h2><div class="table-wrap" id="intelRecommendations"></div></article><div class="grid two section"><article class="card"><h2>Saved strategy intelligence</h2><div class="table-wrap" id="intelStrategies"></div></article><article class="card"><h2>Evidence-backed tips and hypotheses</h2><div class="timeline" id="intelLearnings"></div></article></div><article class="card section"><h2>Recently captured evidence</h2><div class="table-wrap" id="intelEvidence"></div></article></section>
<section id="operations" class="view"><div class="grid two"><article class="card"><h2>Service health and upstream feeds</h2><div id="sources" class="source-list"></div></article><article class="card"><h2>Current blockers and errors</h2><div class="timeline" id="errors"></div></article></div><article class="card section"><h2>Raw freshness timestamps</h2><div id="freshness" class="source-list"></div></article></section>
<footer class="muted" style="padding:18px 2px">GET-only dashboard. It cannot issue, modify, or close trades. Green source status means a production read succeeded; it does not guarantee profitability or immediate trading.</footer>
</main><script>window.__INITIAL__=__INITIAL_JSON__;
const $=id=>document.getElementById(id), arr=x=>Array.isArray(x)?x:(x&&typeof x==='object'?Object.values(x):[]), n=x=>Number(x||0), esc=x=>String(x??'N/A').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])), money=x=>(n(x)<0?'−':n(x)>0?'+':'')+'$'+Math.abs(n(x)).toFixed(4), pct=x=>n(x).toFixed(2)+'%', cls=x=>n(x)>0?'good':n(x)<0?'bad':'', when=x=>{if(!x)return'N/A';const d=new Date(x);return isNaN(d)?esc(x):d.toLocaleString()}, val=(o,...ks)=>{for(const k of ks)if(o&&o[k]!==undefined&&o[k]!==null&&o[k]!=='')return o[k];return null};
let S=window.__INITIAL__, candidates=[];
function metric(label,value,detail='',tone=''){return `<article class="card metric"><div class="label">${esc(label)}</div><div class="value ${tone}">${value}</div><div class="muted">${esc(detail)}</div></article>`}
function badge(text,tone=''){return `<span class="status ${tone}">${esc(text||'UNKNOWN')}</span>`}
function table(headers,rows,empty='No data reported'){if(!rows.length)return `<div class="empty">${esc(empty)}</div>`;return `<table><thead><tr>${headers.map(x=>`<th>${esc(x)}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${r.map((x,i)=>`<td${i===r.length-1?' class="wrap"':''}>${x}</td>`).join('')}</tr>`).join('')}</tbody></table>`}
function kv(label,value,tone=''){return `<div class="source"><span class="muted">${esc(label)}</span><b class="${tone}">${value}</b></div>`}
function event(title,detail,time='',bad=false){if(title==='Solana blocker'){title='Solana activation requirement';bad=false}return `<div class="event ${bad?'errorbox':''}"><b>${esc(title)}</b><div>${esc(detail)}</div>${time?`<div class="time">${when(time)}</div>`:''}</div>`}
function positions(){const out=[],cb=S.coinbase||{},p=cb.portfolio?.open_position;if(p)out.push(['Coinbase',p.product_id,p.status,val(p,'net_qty')??p.fills?.net_qty,val(p,'entry_price'),val(p,'mark_price'),p.net_unrealized_pnl_usdc]);const fx=S.forex||{},broker=fx.broker||fx.reconciliation||fx.account||{};for(const x of arr(fx.open_positions||broker.open_trades||broker.positions))out.push(['OANDA',val(x,'instrument','symbol','pair'),val(x,'side')||(n(x.units)>0?'BUY':'SELL'),val(x,'units','quantity'),val(x,'entry_price','price'),val(x,'mark_price','current_price'),val(x,'unrealized_pl','unrealized_pnl_usd')]);for(const x of arr((S.solana||{}).paperPositions))out.push(['Solana paper',val(x,'symbol','mint'),'PAPER',x.quantity,x.entryUsd,val(x,'markUsd','markPrice'),n(val(x,'markUsd','markPrice'))-n(x.entryUsd)]);const ma=S.multi_asset||{},mw=ma.multi_week_crypto||{},mwp=arr(mw.open_positions||ma.open_positions).filter(x=>!x.strategy||String(x.strategy).includes('MULTI_WEEK_CRYPTO'));for(const x of mwp)out.push(['Multi-week paper',val(x,'symbol','contract'),'PAPER HOLD',x.quantity,x.entry_price,x.current_mark_price,x.current_unrealized_pnl_usd]);return out}
function renderOverview(){const cb=S.coinbase||{},ctrl=cb.portfolio?.controls||{},fx=S.forex||{},broker=fx.broker||fx.account||fx.reconciliation||{},sol=S.solana||{},ma=S.multi_asset||{},mw=ma.multi_week_crypto||{},fxNav=val(broker,'NAV','nav')??fx.nav,fxPnl=val(broker,'realized_pl')??fx.realized_pnl_usd,cbVal=ctrl.equity_usdc??cb.permitted_capital_usdc,mwOpenPnl=arr(mw.open_positions||ma.open_positions).reduce((a,x)=>a+n(x.current_unrealized_pnl_usd),0);const ps=positions(),up=Object.values(S.sources||{}).filter(x=>x.ok).length,total=Object.keys(S.sources||{}).length;$('metrics').innerHTML=metric('Coinbase equity',money(cbVal),`${money(cb.realized_pnl_usdc)} realized`,cls(cb.realized_pnl_usdc))+metric('OANDA NAV',money(fxNav),`${money(fxPnl)} realized`,cls(fxPnl))+metric('Multi-week open P&L',money(mwOpenPnl),'Paper · unrealized',cls(mwOpenPnl))+metric('Open positions',ps.length,'Across all systems')+metric('Scanner candidates',arr(S.discovery?.candidates).length,`${arr(S.discovery?.candidates).filter(x=>x.qualified||x.paper_qualified).length} qualified`)+metric('Service feeds',`${up}/${total}`,up===total?'All responding':'Attention required',up===total?'good':'bad');$('positions').innerHTML=table(['System','Asset','Side','Qty','Entry','Mark','Unrealized P&L'],ps.map(r=>r.map((x,i)=>i===6?`<b class="${cls(x)}">${money(x)} · NOT FINAL</b>`:esc(x))));$('readiness').innerHTML=Object.entries(S.sources||{}).map(([k,v])=>kv(k.toUpperCase(),badge(v.ok?'ONLINE':'FAILED',v.ok?'good':'bad'))).join('')+kv('Coinbase mode',badge(cb.mode,cb.mode?.includes('LIVE')?'good':'warn'))+kv('Forex mode',badge(fx.mode,fx.mode?.includes('LIVE')?'good':'warn'))+kv('Solana live',badge(sol.live?'ARMED':'DISABLED',sol.live?'warn':''));const acts=[...arr(sol.paperFills).slice(0,5),...arr(fx.recentActions).slice(0,5),...arr(mw.recent_actions).slice(0,5)].sort((a,b)=>Date.parse(val(b,'at','time','closed_at','recorded_at')||0)-Date.parse(val(a,'at','time','closed_at','recorded_at')||0)).slice(0,8);$('activity').innerHTML=acts.length?acts.map(x=>event(`${x.action||x.type||'ACTION'} ${x.symbol||x.instrument||''}`,x.reason||x.entryReason||`${x.strategy||''} ${x.realizedPnlUsd!==undefined?money(x.realizedPnlUsd):''}`,val(x,'at','time','closed_at','recorded_at'))).join(''):'<div class="empty">No recent confirmed or paper trade actions.</div>';const problems=[];for(const [k,v] of Object.entries(S.sources||{}))if(!v.ok)problems.push(event(`${k} feed failed`,v.error||'No data returned','',true));for(const x of arr(sol.blockers))problems.push(event('Solana blocker',x,'',true));if(fx.last_error)problems.push(event('Forex error',fx.last_error,fx.last_scan,true));if((ma.held_position_monitor||{}).status==='DEGRADED')problems.push(event('Multi-week monitor degraded','Executable marks are missing or stale',ma.held_position_monitor?.last_refresh_at,true));$('attention').innerHTML=problems.join('')||event('No critical dashboard-source failure','All configured read-only feeds are responding.');}
function renderCoinbase(){const cb=S.coinbase||{},p=cb.portfolio||{},c=p.controls||{},op=p.open_position;$('cbMetrics').innerHTML=metric('Mode',esc(cb.mode||'N/A'),'Execution posture')+metric('Equity',money(c.equity_usdc||cb.permitted_capital_usdc),'Risk equity')+metric('Realized P&L',money(cb.realized_pnl_usdc),'Cumulative',cls(cb.realized_pnl_usdc))+metric('Drawdown',pct(c.drawdown_pct),'Peak to current',n(c.drawdown_pct)>4?'bad':'')+metric('Allocation',pct(n(c.recommended_allocation_fraction)*100),'Recommended maximum')+metric('Open position',op?'1':'0',op?.product_id||'Flat');$('cbState').innerHTML=table(['Field','Current value'],[['Product',esc(op?.product_id)],['Status',esc(op?.status)],['Entry',esc(op?.entry_price)],['Mark',esc(op?.mark_price)],['Unrealized',`<b class="${cls(op?.net_unrealized_pnl_usdc)}">${money(op?.net_unrealized_pnl_usdc)}</b>`],['Recovery posture',esc(c.recovery_mode)],['Risk multiplier',esc(c.risk_multiplier)],['Score boost',esc(c.minimum_score_boost)],['New entries halted',badge(c.new_entries_halted?'YES':'NO',c.new_entries_halted?'bad':'good')]]);const ev=arr(cb.events||cb.recent_events||p.events).slice(0,15);$('cbEvents').innerHTML=ev.length?ev.map(x=>event(val(x,'event_type','type','name')||'EVENT',JSON.stringify(x.payload||x.detail||x).slice(0,260),val(x,'created_at','at','timestamp'))).join(''):'<div class="empty">No event history exposed by the current Coinbase snapshot.</div>';$('cbReview').textContent=JSON.stringify(cb.model_review||cb.latest_model_review||p.model_review||{message:'No model-review payload exposed.'},null,2)}
function renderForex(){const f=S.forex||{},b=f.broker||f.account||f.reconciliation||{},open=arr(f.open_positions||b.open_trades||b.positions),sum=b.summary||f.summary||b;$('fxMetrics').innerHTML=metric('Mode',esc(f.mode||'N/A'),'Executor mode')+metric('NAV',money(val(sum,'NAV','nav')||f.nav),'Broker account value')+metric('Realized P&L',money(val(sum,'realized_pl')??f.realized_pnl_usd),'Broker confirmed',cls(val(sum,'realized_pl')??f.realized_pnl_usd))+metric('Open trades',open.length,'Broker confirmed')+metric('Margin used',money(val(sum,'margin_used','marginUsed')),'Current')+metric('Last scan',f.last_scan?when(f.last_scan):'N/A',f.executor_ready?'Executor ready':'Not ready',f.executor_ready?'good':'bad');$('fxPositions').innerHTML=table(['Pair','Units','Entry','Current','Unrealized','Stop / target'],open.map(x=>[esc(val(x,'instrument','symbol')),esc(val(x,'units','quantity')),esc(val(x,'price','entry_price')),esc(val(x,'current_price','mark_price')),`<b class="${cls(val(x,'unrealizedPL','unrealized_pl'))}">${money(val(x,'unrealizedPL','unrealized_pl'))}</b>`,esc(`${val(x,'stopLossOrder','stop_price')||'N/A'} / ${val(x,'takeProfitOrder','target_price')||'N/A'}`)]));const risk=f.risk_configuration||f.risk||{};$('fxRisk').innerHTML=Object.entries(risk).slice(0,14).map(([k,v])=>kv(k.replaceAll('_',' '),esc(typeof v==='object'?JSON.stringify(v):v))).join('')||kv('Risk payload','Not exposed');const intents=arr(f.intents||f.snapshots||f.outcomes);$('fxIntents').innerHTML=table(['Time','Pair','Direction','Score','Decision','Reason'],intents.slice(0,30).map(x=>[when(val(x,'created_at','at','timestamp')),esc(val(x,'symbol','instrument')),esc(val(x,'direction','side')),esc(val(x,'score','signal_score')),badge(val(x,'status','decision','outcome')||'OBSERVED',String(val(x,'status','decision','outcome')).match(/fill|accept|open/i)?'good':'warn'),esc(val(x,'reason','rejection_reason','detail')||'No reason exposed')]))}
function strategyRows(){const s=S.solana||{},raw=s.strategyPerformance||{};let rows=arr(raw);if(!rows.length)rows=[s.divineV3Performance,s.controlV2Performance,s.explorationPaperStats].filter(Boolean);return rows.map(x=>[x.strategy||x.version||'Strategy',x.version||'',n(x.closed),n(x.open),pct(x.winRatePct),money(val(x,'costStressedPnlUsd','rawPnlUsd')),money(x.expectancyUsd)])}
function renderSolana(){const s=S.solana||{},bal=s.balances||{},stats=s.explorationPaperStats||{};$('solMetrics').innerHTML=metric('Paper P&L',money(s.paperRealizedPnlUsd),'Simulated',cls(s.paperRealizedPnlUsd))+metric('Observations',n(s.paperObservations),'Closed paper outcomes')+metric('Open paper',arr(s.paperPositions).length,'Current simulated positions')+metric('Paper win rate',pct(stats.winRatePct),'Exploration strategy')+metric('Wallet USDC',money(bal.usdc),'Live wallet balance')+metric('Live execution',s.live?'ARMED':'DISABLED',s.ready?'Ready':'Blocked',s.live&&s.ready?'warn':'');$('solPositions').innerHTML=table(['Token','Strategy','Opened','Entry','Mark','Return','Score','Entry evidence'],arr(s.paperPositions).map(x=>{const ret=(n(x.markUsd)-n(x.entryUsd))/Math.max(n(x.entryUsd),.00001)*100;return[esc(x.symbol||x.mint),esc(`${x.strategy||''} ${x.strategyVersion||''}`),when(x.openedAt),money(x.entryUsd),money(x.markUsd),`<b class="${cls(ret)}">${pct(ret)}</b>`,esc(x.score),esc(x.entryReason||JSON.stringify(x.entryEvidence||{}))]}));$('solActions').innerHTML=table(['Time','Action','Token','Strategy','P&L','Return','Reason'],arr(s.paperFills).slice(0,40).map(x=>[when(x.at),badge(x.action,x.action==='SELL'?cls(x.realizedPnlUsd):'info'),esc(x.symbol||x.mint),esc(`${x.strategy||''} ${x.strategyVersion||''}`),`<b class="${cls(x.realizedPnlUsd)}">${x.realizedPnlUsd===undefined?'—':money(x.realizedPnlUsd)}</b>`,x.returnPct===undefined?'—':pct(n(x.returnPct)*100),esc(x.reason||x.entryReason)]));$('strategies').innerHTML=table(['Strategy','Version','Closed','Open','Win rate','Stressed P&L','Expectancy'],strategyRows().map(r=>r.map((x,i)=>i>=5?`<b class="${cls(parseFloat(String(x).replace(/[^0-9.-]/g,'')))}">${x}</b>`:esc(x))));const d=S.discovery||{};$('walletIntel').innerHTML=kv('Tracked wallet events',esc(d.wallet_events||s.walletEvents||0))+kv('Qualified-wallet evidence',esc(arr(d.candidates).reduce((a,x)=>a+n(x.qualified_wallet_count),0)))+kv('Expected wallet',esc(s.walletSuffix?`••••${s.walletSuffix}`:'Not exposed'))+kv('Wallet verified',badge(s.walletIdentityVerified?'YES':'NO',s.walletIdentityVerified?'good':'warn'))+kv('Last discovery',esc(when(s.lastSuccessfulDiscoveryAt||d.scanned_at)))+kv('Discovery feed',esc(d.feed||'N/A'))}
function renderMultiWeek(){const ma=S.multi_asset||{},mw=ma.multi_week_crypto||{},mon=ma.held_position_monitor||{},state=ma.worker_state||{},ps=arr(mw.open_positions||ma.open_positions).filter(x=>!x.strategy||String(x.strategy).includes('MULTI_WEEK_CRYPTO')),openPnl=ps.reduce((a,x)=>a+n(x.current_unrealized_pnl_usd),0),exposure=ps.reduce((a,x)=>a+n(x.entry_value_usd),0),stale=ps.filter(x=>x.monitoring_status!=='FRESH').length;$('mwMetrics').innerHTML=metric('Open positions',ps.length,'Paper holds')+metric('Invested',money(exposure),'Paper capital')+metric('Open P&L',money(openPnl),exposure?`${pct(openPnl/exposure*100)} return`:'No exposure',cls(openPnl))+metric('Realized today',money(mw.daily_realized_pnl_usd),'USD paper result',cls(mw.daily_realized_pnl_usd))+metric('Stale marks',stale,stale?'Needs attention':'All fresh',stale?'bad':'good')+metric('Monitor',esc(mon.status||'UNKNOWN'),`${n(state.cycle_count)} cycles`,mon.status==='READY'?'good':'bad');$('mwPositions').innerHTML=table(['Coin','Invested USD','Current USD','P&L USD','Return','Stop','MFE / MAE','Mark state'],ps.map(x=>[esc(x.symbol||x.contract),money(x.entry_value_usd),money(x.current_value_usd),`<b class="${cls(x.current_unrealized_pnl_usd)}">${money(x.current_unrealized_pnl_usd)}</b>`,`<b class="${cls(x.return_pct)}">${pct(x.return_pct)}</b>`,esc(x.stop_price),`${money(x.mfe_usd)} / ${money(x.mae_usd)}`,badge(x.monitoring_status,x.monitoring_status==='FRESH'?'good':'bad')]));$('mwMonitor').innerHTML=kv('Status',badge(mon.status||'UNKNOWN',mon.status==='READY'?'good':'bad'))+kv('Refresh interval',`${esc(mon.refresh_interval_seconds)} seconds`)+kv('Last refresh',when(mon.last_refresh_at))+kv('Fresh quotes',esc(mon.fresh_quote_count))+kv('Restored positions',esc(state.restored_open_position_count))+kv('Last successful cycle',when(state.last_successful_cycle_at))+kv('Persistence',badge(state.persistence_configured?'CONFIGURED':'MISSING',state.persistence_configured?'good':'bad'));$('mwActions').innerHTML=table(['Time','Action','Coin','P&L USD','Tier / reason'],arr(mw.recent_actions).map(x=>[when(x.recorded_at),badge(x.type,String(x.type).includes('CLOSE')?cls(x.realized_pnl_usd):'info'),esc(x.symbol||x.contract),x.realized_pnl_usd===undefined?'—':`<b class="${cls(x.realized_pnl_usd)}">${money(x.realized_pnl_usd)}</b>`,esc(x.profit_tier||x.reason||x.entry_stage)]));const ec=arr(ma.emerging_discovery?.candidates).filter(x=>x.research_eligible).slice(0,20);$('mwCandidates').innerHTML=table(['Chain','Coin','Research score','Confirmations','Safety','Main blockers'],ec.map(x=>[esc(x.chain),esc(x.symbol),esc(x.research_score),esc(x.confirmation_count),badge(x.security_verified?'VERIFIED':'UNVERIFIED',x.security_verified?'good':'warn'),esc(arr(x.failures).slice(0,3).join('; ')||'None')]));}
function renderMarket(){candidates=arr(S.discovery?.candidates);$('scanSummary').textContent=`${candidates.length} candidates · ${candidates.filter(x=>x.qualified).length} live-qualified · ${candidates.filter(x=>x.paper_qualified).length} paper-qualified · ${candidates.filter(x=>x.shadow_qualified).length} shadow-qualified · ${when(S.discovery?.scanned_at)}`;renderCandidateRows();const diag=S.discovery?.strategy_diagnostics||S.solana?.discoveryDiagnostics||{};$('scanDiagnostics').innerHTML=Object.entries(diag).map(([k,v])=>kv(k.replaceAll('_',' '),esc(typeof v==='object'?JSON.stringify(v):v))).join('')||kv('Diagnostics','No diagnostic payload');$('provenance').innerHTML=kv('Feed',esc(S.discovery?.feed))+kv('Scan status',badge(S.discovery?.scan_status,S.discovery?.ok?'good':'bad'))+kv('Wallet events',esc(S.discovery?.wallet_events||0))+kv('Scanned at',esc(when(S.discovery?.scanned_at)))+kv('Error',esc(S.discovery?.error||'None'),S.discovery?.error?'bad':'good')}
function renderCandidateRows(){const q=($('candidateFilter').value||'').toLowerCase(),xs=candidates.filter(x=>JSON.stringify(x).toLowerCase().includes(q)).slice(0,100);$('candidateRows').innerHTML=xs.map(x=>{const reasons=arr(x.paper_failures||x.rejection_reasons||x.rejections||x.blockers),decision=x.qualified?'LIVE QUALIFIED':x.paper_qualified?'PAPER QUALIFIED':x.shadow_qualified?'SHADOW QUALIFIED':reasons[0]||'OBSERVING';return `<tr><td><b>${esc(x.symbol||x.name||x.mint)}</b></td><td>${esc(x.strategy)}</td><td>${esc(x.score)}</td><td>${esc(x.age_minutes||x.token_age_minutes)}m</td><td>${money(x.liquidity_usd)}</td><td>${esc(x.unique_buyers_5m)}</td><td>${pct(n(x.net_buy_pressure)*100)}</td><td>${esc(x.qualified_wallet_count||0)}</td><td class="wrap">${badge(decision,x.qualified?'good':x.paper_qualified||x.shadow_qualified?'warn':'')} ${esc(reasons.slice(0,3).join('; '))}</td></tr>`}).join('')||'<tr><td colspan="9" class="empty">No matching candidates.</td></tr>'}
function renderPerformance(){const rows=strategyRows();$('performanceTable').innerHTML=table(['Strategy','Version','Closed','Open','Win rate','P&L','Expectancy'],rows.map(r=>r.map((x,i)=>i>=5?`<b class="${cls(parseFloat(String(x).replace(/[^0-9.-]/g,'')))}">${x}</b>`:esc(x))));drawChart(rows.map(r=>({name:`${r[0]} ${r[1]}`,value:parseFloat(String(r[5]).replace(/[^0-9.-]/g,''))||0})));const follow=arr(S.solana?.postExitFollowups||S.solana?.postExitCounterfactuals);$('counterfactuals').innerHTML=table(['Token','Strategy','Closed','Exit','15m','30m','60m','What happened next'],follow.slice(0,30).map(x=>[esc(x.symbol||x.mint),esc(`${x.strategy||''} ${x.strategyVersion||''}`),when(x.closedAt),money(x.exitUsd),esc(x.checkpoints?.['15']||x.checkpoints?.['15m']),esc(x.checkpoints?.['30']||x.checkpoints?.['30m']),esc(x.checkpoints?.['60']||x.checkpoints?.['60m']),esc(JSON.stringify(x.checkpoints||{}))]));const evidence=[];for(const r of rows)evidence.push(event(`${r[0]} ${r[1]}`,`${r[2]} closed · ${r[4]} win rate · ${r[6]} expectancy`));const diag=S.solana?.discoveryDiagnostics||{};for(const [k,v] of Object.entries(diag).slice(0,8))evidence.push(event(k,typeof v==='object'?JSON.stringify(v):String(v)));$('modelEvidence').innerHTML=evidence.join('')||'<div class="empty">Insufficient model evidence.</div>'}
function renderIntelligence(){const i=S.intelligence||{},t=i.totals||{},rs=i.recommendation_summary||{};$('intelMetrics').innerHTML=metric('Evidence records',n(t.observations),'Persistent observations')+metric('Strategies',n(t.strategies),'Forex and crypto')+metric('Shadow tests',n(t.shadow_observations),'No live capital')+metric('Action items',n(rs.total),`${n(rs.p0)} urgent · ${n(rs.p1)} experiments`)+metric('Hard controls',n(t.hard_reject_controls),'Unsafe or unsellable')+metric('Checkpoints','5–240m','Executable forward paths');$('intelRecommendations').innerHTML=table(['Priority','Category','Strategy','Action','Evidence / gate'],arr(i.actionable_recommendations).map(x=>[badge(x.priority,x.priority==='P0'?'bad':x.priority==='P1'?'warn':'good'),esc(x.category),esc(x.strategy),esc(x.action),`${esc(x.rationale)}<br><span class="muted">n=${esc(x.sample_size)} · ${esc(x.confidence)} · ${esc(x.adoption_threshold)}</span>`]));$('intelStrategies').innerHTML=table(['Strategy','Evidence','Closed','Wins','Expectancy','Stressed expectancy'],arr(i.strategies).map(x=>[esc(x.strategy),esc(x.observations),esc(x.closed),esc(x.wins),`<b class="${cls(x.expectancy_usd)}">${money(x.expectancy_usd)}</b>`,`<b class="${cls(x.cost_stressed_expectancy_usd)}">${money(x.cost_stressed_expectancy_usd)}</b>`]));$('intelLearnings').innerHTML=arr(i.learnings).map(x=>event(`${x.mechanism} · ${x.status}`,`${x.statement} Sample ${x.sample_size}; gate: ${x.adoption_threshold}`,x.created_at,x.status==='DO_NOT_PROMOTE')).join('')||'<div class="empty">Learning extraction is awaiting evidence.</div>';$('intelEvidence').innerHTML=table(['Observed','Service','Strategy','Cohort','Instrument','Event','P&L'],arr(i.recent_evidence).map(x=>[when(x.observed_at),esc(x.source_service),esc(`${x.strategy} ${x.strategy_version||''}`),badge(x.cohort,x.cohort?.includes('HARD')?'bad':x.cohort?.includes('SHADOW')?'warn':'good'),esc(x.instrument),esc(x.event_type),x.realized_pnl_usd===null?'—':`<b class="${cls(x.realized_pnl_usd)}">${money(x.realized_pnl_usd)}</b>`]))}
function drawChart(data){const c=$('pnlChart'),x=c.getContext('2d'),w=c.width,h=c.height;x.clearRect(0,0,w,h);x.strokeStyle='#2a405a';x.beginPath();x.moveTo(45,h/2);x.lineTo(w-12,h/2);x.stroke();if(!data.length){x.fillStyle='#8fa3bc';x.fillText('No strategy data',20,30);return}const max=Math.max(...data.map(d=>Math.abs(d.value)),.01),bw=(w-70)/data.length*.62;data.forEach((d,i)=>{const cx=55+(i+.5)*(w-70)/data.length,bh=Math.abs(d.value)/max*(h*.36),y=d.value>=0?h/2-bh:h/2;x.fillStyle=d.value>=0?'#31d08b':'#ff6577';x.fillRect(cx-bw/2,y,bw,bh);x.fillStyle='#8fa3bc';x.font='11px sans-serif';x.fillText(d.name.slice(0,15),cx-bw/2,h-12);x.fillStyle='#e8f0fa';x.fillText(money(d.value),cx-bw/2,d.value>=0?y-6:y+bh+14)})}
function renderOps(){const src=S.sources||{};$('sources').innerHTML=Object.entries(src).map(([k,v])=>kv(k.toUpperCase(),`${badge(v.ok?'ONLINE':'FAILED',v.ok?'good':'bad')} <span class="muted">${esc(v.source||'no source')}</span>`)).join('');const er=[];for(const [k,v] of Object.entries(src))if(v.error)er.push(event(`${k} upstream`,v.error,'',true));for(const x of arr(S.solana?.errors))er.push(event('Solana executor',x,'',true));if(S.solana?.discoveryError)er.push(event('Solana discovery',S.solana.discoveryError,'',true));if(S.forex?.last_error)er.push(event('Forex executor',S.forex.last_error,S.forex.last_scan,true));if(S.multi_asset?.last_error)er.push(event('Multi-week worker',S.multi_asset.last_error,S.multi_asset.last_scan,true));$('errors').innerHTML=er.join('')||event('No current service error','All dashboard feeds returned usable data.');$('freshness').innerHTML=kv('Dashboard snapshot',when(S.generated_at))+kv('Forex scan',when(S.forex?.last_scan))+kv('Solana discovery',when(S.solana?.lastSuccessfulDiscoveryAt||S.discovery?.scanned_at))+kv('Multi-week scan',when(S.multi_asset?.last_scan))+kv('Multi-week marks',when(S.multi_asset?.held_position_monitor?.last_refresh_at))+kv('Discovery scan status',esc(S.discovery?.scan_status||'N/A'))}
function render(){renderOverview();renderCoinbase();renderForex();renderSolana();renderMultiWeek();renderMarket();renderPerformance();renderIntelligence();renderOps();$('updated').textContent='Updated '+new Date(S.generated_at).toLocaleTimeString();$('updated').classList.add('flash');setTimeout(()=>$('updated').classList.remove('flash'),800)}
async function refresh(){try{const r=await fetch('/dashboard/data',{cache:'no-store'});if(!r.ok)throw Error(`HTTP ${r.status}`);S=await r.json();$('connection').textContent='CONNECTED';$('connection').className='pill good';$('pulse').style.background='var(--green)';render()}catch(e){$('connection').textContent='STALE · '+e.message;$('connection').className='pill bad';$('pulse').style.background='var(--red)'}}
$('nav').addEventListener('click',e=>{if(!e.target.dataset.view)return;document.querySelectorAll('.nav button,.view').forEach(x=>x.classList.remove('active'));e.target.classList.add('active');$(e.target.dataset.view).classList.add('active');if(e.target.dataset.view==='performance')renderPerformance()});$('candidateFilter').addEventListener('input',renderCandidateRows);render();setInterval(refresh,__REFRESH__*1000);setTimeout(refresh,1500);
</script></body></html>'''
    return page.replace("__INITIAL_JSON__", initial).replace("__REFRESH__", str(refresh))
