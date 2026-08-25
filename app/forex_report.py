from __future__ import annotations

import html
from datetime import datetime, timezone


def _money(value: object) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def _pct(value: object) -> str:
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return "0.00%"


def render_forex_report(report: dict) -> str:
    stamp = str(report.get("generated_at") or datetime.now(timezone.utc).isoformat())
    mode = html.escape(str(report.get("mode") or "UNKNOWN"))
    ready = bool(report.get("executor_ready"))
    broker = report.get("broker") or {}
    snapshots = report.get("snapshots") or []
    outcomes = report.get("outcomes") or []
    intents = report.get("intents") or []
    events = report.get("events") or []
    review = report.get("model_review") or {}
    risk = report.get("risk_configuration") or {}
    balance = float(broker.get("balance") or 0)
    nav = float(broker.get("nav") or balance)
    baseline = float(report.get("capital_baseline_nav") or report.get("baseline_nav") or nav)
    pnl = nav - baseline
    daily_baseline = float(report.get("daily_baseline_nav") or nav)
    daily_pnl = nav - daily_baseline
    margin = float(broker.get("margin_used") or 0)
    pending = int(report.get("pending_order_count") or 0)
    calendar_ok = sum(1 for row in snapshots if row.get("calendar_verified") is True)
    status_color = "#16a34a" if ready else "#dc2626"

    def rows(values: list[dict], columns: list[tuple[str, str]]) -> str:
        if not values:
            return '<tr><td colspan="9" style="padding:12px;color:#64748b">No records</td></tr>'
        rendered = []
        for index, value in enumerate(values):
            cells = "".join(f'<td style="padding:10px;border-bottom:1px solid #e2e8f0">{html.escape(str(value.get(key, "")))}</td>' for key, _ in columns)
            rendered.append(f'<tr style="background:{"#f8fafc" if index % 2 else "#ffffff"}">{cells}</tr>')
        return "".join(rendered)

    snapshot_rows = []
    for item in snapshots:
        snapshot_rows.append({
            "symbol": item.get("symbol", ""), "price": item.get("price", ""),
            "1h": _pct(item.get("change_1h_pct")), "24h": _pct(item.get("change_24h_pct")),
            "spread": f'{float(item.get("spread_bps") or 0):.1f} bps',
            "calendar": "VERIFIED" if item.get("calendar_verified") is True else "BLOCKED",
        })
    outcome_rows = [{"symbol": x.get("symbol", ""), "status": x.get("status", ""),
                     "score": f'{float(x.get("score") or 0):.2f} / {float(x.get("minimum_score") or 0):.2f}',
                     "reason": x.get("reason", x.get("id", ""))} for x in outcomes]
    intent_rows = [{"created": x.get("created_at", ""), "symbol": x.get("symbol", ""), "side": x.get("side", ""),
                    "mode": x.get("mode", ""), "status": x.get("status", ""),
                    "score": x.get("score", ""), "entry": x.get("entry_price", ""),
                    "stop": x.get("stop_price", ""), "target": x.get("target_price", ""),
                    "risk": _money(x.get("maximum_loss_usd")), "pnl": _money(x.get("realized_pnl_usd"))} for x in intents]
    event_rows = [{"time": x.get("recorded_at", ""), "type": x.get("type", ""),
                   "hash": str(x.get("record_hash", ""))[:12]} for x in events]

    return f'''<!doctype html><html><body style="margin:0;background:#eef2ff;font-family:Arial,sans-serif;color:#0f172a">
<div style="max-width:920px;margin:0 auto;padding:16px">
<div style="background:#172554;color:white;border-radius:16px;padding:24px"><div style="font-size:12px;opacity:.8">FOREX INTELLIGENCE • {html.escape(stamp)}</div><h1 style="margin:8px 0">Production Forex Dashboard</h1><span style="display:inline-block;background:{status_color};padding:8px 14px;border-radius:999px;font-weight:bold">{mode} • {"READY" if ready else "NOT READY"}</span></div>
<div style="display:flex;flex-wrap:wrap;gap:12px;margin:16px 0">
<div style="flex:1;min-width:180px;background:white;padding:16px;border-radius:12px"><small>Account NAV</small><div style="font-size:24px;font-weight:bold">{_money(nav)}</div></div>
<div style="flex:1;min-width:180px;background:white;padding:16px;border-radius:12px"><small>P&amp;L vs baseline</small><div style="font-size:24px;font-weight:bold;color:{'#16a34a' if pnl >= 0 else '#dc2626'}">{_money(pnl)}</div></div>
<div style="flex:1;min-width:180px;background:white;padding:16px;border-radius:12px"><small>Daily NAV change</small><div style="font-size:24px;font-weight:bold;color:{'#16a34a' if daily_pnl >= 0 else '#dc2626'}">{_money(daily_pnl)}</div></div>
<div style="flex:1;min-width:180px;background:white;padding:16px;border-radius:12px"><small>Margin used</small><div style="font-size:24px;font-weight:bold">{_money(margin)}</div></div>
<div style="flex:1;min-width:180px;background:white;padding:16px;border-radius:12px"><small>Calendar coverage</small><div style="font-size:24px;font-weight:bold">{calendar_ok}/{len(snapshots)}</div></div>
</div>
<div style="background:white;border-radius:12px;padding:18px;margin-bottom:14px"><h2>Risk and readiness</h2><p><b>Execution:</b> {"Enabled by all independent gates" if mode == "LIVE_ARMED" else "No live order authorization"}. <b>Open broker trades:</b> {int(broker.get("open_trade_count") or report.get("open_trade_count") or 0)}. <b>Pending orders:</b> {pending}. <b>Margin available:</b> {_money(broker.get("margin_available"))}. <b>Realized P&amp;L:</b> {_money(broker.get("realized_pl", report.get("realized_pnl_usd")))}. <b>Unrealized P&amp;L:</b> {_money(broker.get("unrealized_pl"))}. <b>Financing:</b> {_money(broker.get("financing"))}. <b>Last scan:</b> {html.escape(str(report.get("last_scan") or "never"))}.</p><p style="color:#b91c1c"><b>Current error:</b> {html.escape(str(report.get("last_error") or "None"))}</p></div>
<div style="background:white;border-radius:12px;padding:18px;margin-bottom:14px"><h2>Effective guardrails</h2><p><b>Minimum score:</b> {html.escape(str(risk.get("minimum_score", "N/A")))}. <b>Position limit:</b> {html.escape(str(risk.get("maximum_open_positions", "N/A")))}. <b>Risk per trade:</b> {_money(risk.get("maximum_risk_per_trade_usd"))}. <b>Open / maximum combined risk:</b> {_money(risk.get("current_open_risk_usd"))} / {_money(risk.get("maximum_combined_risk_usd"))}. <b>Daily loss limit:</b> {_money(risk.get("daily_loss_limit_usd"))}. <b>Notional limit:</b> {_money(risk.get("maximum_notional_usd"))}. <b>Margin limit:</b> {_money(risk.get("maximum_margin_used_usd"))}. <b>Currency-overlap guard:</b> {"ON" if risk.get("currency_overlap_guard") else "OFF"}.</p></div>
<div style="background:white;border-radius:12px;padding:18px;margin-bottom:14px;overflow-x:auto"><h2>Market and calendar</h2><table style="border-collapse:collapse;width:100%;font-size:13px"><tr><th>Pair</th><th>Price</th><th>1h</th><th>24h</th><th>Spread</th><th>Calendar</th></tr>{rows(snapshot_rows, [("symbol","Pair"),("price","Price"),("1h","1h"),("24h","24h"),("spread","Spread"),("calendar","Calendar")])}</table></div>
<div style="background:white;border-radius:12px;padding:18px;margin-bottom:14px;overflow-x:auto"><h2>Latest decisions</h2><table style="border-collapse:collapse;width:100%;font-size:13px"><tr><th>Pair</th><th>Action</th><th>Score / minimum</th><th>Reason or signal ID</th></tr>{rows(outcome_rows, [("symbol","Pair"),("status","Action"),("score","Score"),("reason","Reason")])}</table></div>
<div style="background:white;border-radius:12px;padding:18px;margin-bottom:14px;overflow-x:auto"><h2>Auditable positions and intents</h2><table style="border-collapse:collapse;width:100%;font-size:13px"><tr><th>Created</th><th>Pair</th><th>Side</th><th>Mode</th><th>Status</th><th>Score</th><th>Entry</th><th>Stop</th><th>Target</th><th>Max loss</th><th>Realized P&amp;L</th></tr>{rows(intent_rows, [("created","Created"),("symbol","Pair"),("side","Side"),("mode","Mode"),("status","Status"),("score","Score"),("entry","Entry"),("stop","Stop"),("target","Target"),("risk","Max loss"),("pnl","P&amp;L")])}</table></div>
<div style="background:white;border-radius:12px;padding:18px;margin-bottom:14px;overflow-x:auto"><h2>Append-only audit trail</h2><table style="border-collapse:collapse;width:100%;font-size:13px"><tr><th>Time</th><th>Event</th><th>Record hash</th></tr>{rows(event_rows, [("time","Time"),("type","Event"),("hash","Hash")])}</table></div>
<div style="background:white;border-radius:12px;padding:18px;margin-bottom:14px"><h2>Model quality and guarded improvement</h2><p><b>Version:</b> {html.escape(str(review.get("model_version") or "FOREX_TREND_1.1"))}. <b>Closed sample:</b> {int(review.get("sample_size") or 0)}. <b>Wins/losses:</b> {int(review.get("wins") or 0)}/{int(review.get("losses") or 0)}. <b>Net expectancy:</b> {_money(review.get("net_expectancy_usd"))}. <b>Profit factor:</b> {html.escape(str(review.get("profit_factor") if review.get("profit_factor") is not None else "N/A"))}.</p><p><b>Status:</b> {html.escape(str(review.get("status") or "MODEL LOCKED - COLLECTING EVIDENCE"))}. Parameters are never changed from a handful of trades. A challenger becomes eligible only after the documented sample and prospective-performance gates pass.</p></div>
<div style="background:#fffbeb;border:1px solid #f59e0b;border-radius:12px;padding:16px"><b>Risk notice</b><p style="margin-bottom:0">No strategy can guarantee profit. Live Forex trading can lose money rapidly. Broker balances, fills, stops, financing charges and open trades must be independently verified.</p></div>
</div></body></html>'''
