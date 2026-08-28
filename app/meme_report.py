from __future__ import annotations

import html
import json
from datetime import datetime, timezone


def esc(value: object, default: str = "N/A") -> str:
    text = default if value is None or value == "" else str(value)
    return html.escape(text)


def money(value: object) -> str:
    try:
        return f"${float(value):,.4f}"
    except (TypeError, ValueError):
        return "$0.0000"


def number(value: object, digits: int = 2, default: str = "N/A") -> str:
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return default


def yes_no(value: object) -> str:
    return "Yes" if bool(value) else "No"


def _event_detail(event: dict) -> str:
    payload = event.get("payload") or {}
    if not isinstance(payload, dict) or not payload:
        return ""
    preferred = ("product_id", "reason", "error", "detail", "status", "score", "hour")
    selected = {key: payload[key] for key in preferred if payload.get(key) not in (None, "")}
    if not selected:
        selected = dict(list(payload.items())[:3])
    return esc(json.dumps(selected, separators=(", ", ": "), default=str)[:300])


def _row(label: str, value: object, label2: str = "", value2: object = "") -> str:
    cells = (f'<td style="padding:9px 11px;color:#64748b;border-bottom:1px solid #e5e7eb">{esc(label)}</td>'
             f'<td style="padding:9px 11px;font-weight:700;border-bottom:1px solid #e5e7eb">{value}</td>')
    if label2:
        cells += (f'<td style="padding:9px 11px;color:#64748b;border-bottom:1px solid #e5e7eb">{esc(label2)}</td>'
                  f'<td style="padding:9px 11px;font-weight:700;border-bottom:1px solid #e5e7eb">{value2}</td>')
    else:
        cells += '<td colspan="2" style="border-bottom:1px solid #e5e7eb"></td>'
    return f"<tr>{cells}</tr>"


def _entry_reason(payload: dict) -> str:
    components = payload.get("component_scores") or {}
    strongest = sorted(((str(k), float(v)) for k, v in components.items()
                        if isinstance(v, (int, float))), key=lambda item: item[1], reverse=True)[:2]
    leaders = ", ".join(f"{name} {value:.1f}" for name, value in strongest)
    if not payload:
        return "Historical record: entry reason was not stored"
    parts = [f"Score {number(payload.get('score'))}"]
    if payload.get("market_regime") or payload.get("regime"):
        parts.append(f"regime {payload.get('market_regime') or payload.get('regime')}")
    if leaders:
        parts.append(f"strongest evidence: {leaders}")
    return "; ".join(parts)


def render_meme_report(report: dict) -> str:
    portfolio = report.get("portfolio") or {}
    position = portfolio.get("open_position")
    controls = portfolio.get("controls") or {}
    review = report.get("model_review") or {}
    deployment = report.get("deployment") or {}
    recommendations = report.get("recommendations") or []
    events = report.get("notification_events") or []
    mode = str(report.get("mode") or "UNKNOWN")
    paused = bool(report.get("paused"))
    breakers = controls.get("circuit_breakers") or []
    ready = mode == "LIVE_ARMED" and not paused and not breakers
    status_color = "#166534" if ready else "#991b1b"
    status_bg = "#dcfce7" if ready else "#fee2e2"
    stamp = esc(report.get("timestamp") or datetime.now(timezone.utc).isoformat())
    alert = report.get("_meme_alert") or {}
    alert_event = alert.get("event") or {}
    alert_payload = alert_event.get("payload") or {}
    alert_kind = str(alert.get("kind") or "")
    alert_ticket_id = str(alert_event.get("ticket_id") or "")
    trade_pnl = alert_payload.get("realized_pnl_usdc")
    if alert_kind:
        result = ("PROFIT" if float(trade_pnl or 0) > 0 else
                  "LOSS" if float(trade_pnl or 0) < 0 else
                  "OPEN — RESULT NOT FINAL")
        alert_html = (f'<tr><td style="padding:20px 26px;background:#fef3c7;border:3px solid #f59e0b">'
                      f'<div style="display:inline-block;background:#dc2626;color:#fff;padding:6px 11px;border-radius:999px;font-size:13px;font-weight:900">NEW ACTION</div>'
                      f'<div style="font-size:12px;color:#92400e;font-weight:700;margin-top:10px">THIS ACTION TRIGGERED THE REPORT</div>'
                      f'<div style="font-size:24px;font-weight:800;margin-top:5px">{esc(alert_kind.replace("_"," "))}</div>'
                      f'<div style="margin-top:8px"><b>This trade:</b> {money(trade_pnl) if trade_pnl is not None else "Not closed yet"} &nbsp; '
                      f'<b>Result:</b> {result} &nbsp; <b>Total realized account P&amp;L:</b> {money(report.get("realized_pnl_usdc"))}</div>'
                      f'<div style="margin-top:6px;color:#475569">Open-position P&amp;L is shown separately below and is not final until the position closes.</div></td></tr>')
    else:
        alert_html = ""

    if position:
        position_rows = "".join([
            _row("Product", esc(position.get("product_id")), "Status", esc(position.get("status"))),
            _row("Entry price", money(position.get("entry_price")), "Mark price", money(position.get("mark_price"))),
            _row("Mark value", money(position.get("mark_value_usdc")), "Unrealized P&L", money(position.get("net_unrealized_pnl_usdc"))),
            _row("High-water price", money(position.get("high_water_price")), "Order ID", esc(position.get("order_id"))),
        ])
    else:
        position_rows = '<tr><td colspan="4" style="padding:14px;color:#475569">No open position. No capital is currently exposed.</td></tr>'

    rec_rows = []
    for item in recommendations[:10]:
        payload = item.get("payload") or {}
        is_new = bool(alert_ticket_id and str(item.get("ticket_id") or payload.get("ticket_id") or "") == alert_ticket_id)
        row_style = "background:#fef3c7;border:3px solid #f59e0b" if is_new else ""
        new_badge = '<span style="display:inline-block;background:#dc2626;color:#fff;padding:3px 7px;border-radius:999px;font-size:11px;font-weight:900;margin-right:5px">NEW</span>' if is_new else ""
        rec_rows.append(
            f'<tr style="{row_style}">'
            f'<td style="padding:8px;border-bottom:1px solid #e5e7eb">{esc(item.get("created_at") or payload.get("created_at"))}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #e5e7eb">{esc(item.get("product_id") or payload.get("product_id"))}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #e5e7eb">{new_badge}{esc(item.get("status"))}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #e5e7eb;text-align:right">{esc(payload.get("score"))}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #e5e7eb">{esc(_entry_reason(payload))}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #e5e7eb">{esc(item.get("exit_reason") or "Open / not recorded")}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #e5e7eb;text-align:right">{money(item.get("max_favorable_pnl"))}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #e5e7eb;text-align:right">{money(item.get("max_adverse_pnl"))}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #e5e7eb;text-align:right">{money(item.get("realized_pnl")) if item.get("realized_pnl") is not None else "N/A"}</td>'
            '</tr>')
    recommendations_html = "".join(rec_rows) or '<tr><td colspan="9" style="padding:12px;color:#64748b">No recent recommendations.</td></tr>'

    event_rows = []
    display_events = []
    routine_counts: dict[str, int] = {}
    for event in events:
        kind = str(event.get("kind") or "")
        if kind in {"PREFLIGHT_OK", "SUPERVISION_OK"}:
            routine_counts[kind] = routine_counts.get(kind, 0) + 1
        else:
            display_events.append(event)
    for kind, count in routine_counts.items():
        first = next(event for event in events if str(event.get("kind") or "") == kind)
        display_events.append({**first, "kind": f"{kind} × {count}",
                               "payload": {"summary": f"{count} identical routine checks aggregated"}})
    display_events.sort(key=lambda item: str(item.get("at") or ""), reverse=True)
    for event in display_events[:20]:
        kind = str(event.get("kind") or "")
        detail_color = "#991b1b" if "ERROR" in kind or "FAILED" in kind else "#475569"
        event_rows.append(
            '<tr>'
            f'<td style="padding:8px;border-bottom:1px solid #e5e7eb;white-space:nowrap">{esc(event.get("at"))}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #e5e7eb;font-weight:700">{esc(kind)}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #e5e7eb;color:{detail_color}">{_event_detail(event)}</td>'
            '</tr>')
    events_html = "".join(event_rows) or '<tr><td colspan="3" style="padding:12px;color:#64748b">No recent audit events.</td></tr>'

    capital_rows = "".join([
        _row("Pilot baseline", money(report.get("baseline_usdc")), "Permitted capital", money(report.get("permitted_capital_usdc"))),
        _row("Realized P&L", money(report.get("realized_pnl_usdc")), "Unrealized P&L", money(position.get("net_unrealized_pnl_usdc")) if position else "$0.0000"),
        _row("Peak drawdown", esc(number(controls.get("drawdown_pct"), default="0.00")) + "%", "Daily drawdown", esc(number(controls.get("daily_drawdown_pct"), default="0.00")) + "%"),
        _row("Consecutive losses", esc(controls.get("consecutive_losses", 0)), "Circuit breakers", esc(", ".join(map(str, breakers)) if breakers else "None")),
        _row("Trading posture", esc(controls.get("recovery_mode", "NORMAL")),
             "Entry sizing", esc(number((controls.get("recommended_allocation_fraction") or .95) * 100, 1)) + "% capital maximum"),
        _row("Risk multiplier", esc(number(controls.get("risk_multiplier", 1), 3)),
             "Extra score required", "+" + esc(number(controls.get("minimum_score_boost", 0), 1)) + " points"),
    ])
    learning_rows = "".join([
        _row("Model", esc(review.get("model_version")), "Status", esc(review.get("status"))),
        _row("Closed sample", esc(review.get("sample_size", 0)), "Wins / losses", f'{esc(review.get("wins", 0))} / {esc(review.get("losses", 0))}'),
        _row("Win rate", esc(number((review.get("win_rate") or 0) * 100)) + "%", "Profit factor", esc(number(review.get("profit_factor"), 4))),
        _row("Net P&L", money(review.get("net_pnl_usdc")), "Expectancy", money(review.get("net_expectancy_usdc"))),
        _row("Average win", money(review.get("average_win_usdc")), "Average loss", money(review.get("average_loss_usdc"))),
        _row("Average MFE", money(review.get("average_max_favorable_excursion_usdc")), "Average MAE", money(review.get("average_max_adverse_excursion_usdc"))),
        _row("Parameters changed", yes_no(review.get("parameters_changed")), "Promotion eligible", yes_no((review.get("promotion_gate") or {}).get("eligible"))),
    ])

    section = 'style="padding:22px 26px 8px;font-size:18px;font-weight:700;color:#172033"'
    table = 'width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #e5e7eb;border-radius:10px;border-collapse:separate;overflow:hidden;font-size:13px"'
    return f'''<!doctype html><html><body style="margin:0;background:#eef2f7;font-family:Arial,Helvetica,sans-serif;color:#0f172a">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef2f7"><tr><td align="center" style="padding:24px 10px">
<table role="presentation" width="760" cellpadding="0" cellspacing="0" style="width:100%;max-width:760px;background:#ffffff;border:1px solid #dbe3ec;border-radius:14px;overflow:hidden">
<tr><td style="padding:26px;background:#111827;color:#fff"><div style="font-size:12px;letter-spacing:1.2px;color:#93c5fd;font-weight:700">CRYPTO LIVE TRADING INTELLIGENCE</div><h1 style="margin:8px 0 5px;font-size:27px">Production Dashboard</h1><div style="color:#cbd5e1;font-size:13px">{stamp}</div></td></tr>
<tr><td style="padding:18px 26px;background:{status_bg};color:{status_color}"><table role="presentation" width="100%"><tr><td><div style="font-size:11px">MODE</div><div style="font-size:20px;font-weight:800">{esc(mode)}</div></td><td><div style="font-size:11px">AUTO EXECUTION</div><div style="font-size:20px;font-weight:800">{'PAUSED' if paused else 'ENABLED'}</div></td><td><div style="font-size:11px">POSITION</div><div style="font-size:20px;font-weight:800">{'OPEN' if position else 'FLAT'}</div></td></tr></table></td></tr>
{alert_html}
<tr><td {section}>Capital and risk controls</td></tr><tr><td style="padding:6px 26px 18px"><table role="presentation" {table}>{capital_rows}</table></td></tr>
<tr><td {section}>Position and protection</td></tr><tr><td style="padding:6px 26px 18px"><table role="presentation" {table}>{position_rows}</table></td></tr>
<tr><td {section}>Recent decisions and completed trades</td></tr><tr><td style="padding:6px 26px 18px;overflow-x:auto"><table role="presentation" {table}><tr style="background:#f8fafc"><th style="padding:8px;text-align:left">Created</th><th style="padding:8px;text-align:left">Product</th><th style="padding:8px;text-align:left">Status</th><th style="padding:8px;text-align:right">Score</th><th style="padding:8px;text-align:left">Main entry reason</th><th style="padding:8px;text-align:left">Exit reason</th><th style="padding:8px;text-align:right">MFE</th><th style="padding:8px;text-align:right">MAE</th><th style="padding:8px;text-align:right">P&L</th></tr>{recommendations_html}</table></td></tr>
<tr><td {section}>Guarded model learning</td></tr><tr><td style="padding:6px 26px 18px"><table role="presentation" {table}>{learning_rows}</table><div style="margin-top:10px;padding:11px;background:#f8fafc;color:#475569;font-size:12px;line-height:18px">Model changes remain locked until the documented sample-size, confidence, cost-stress, and prospective challenger gates pass.</div></td></tr>
<tr><td {section}>Recent audit events and errors</td></tr><tr><td style="padding:6px 26px 22px;overflow-x:auto"><table role="presentation" {table}><tr style="background:#f8fafc"><th style="padding:8px;text-align:left">Time</th><th style="padding:8px;text-align:left">Event</th><th style="padding:8px;text-align:left">Detail</th></tr>{events_html}</table></td></tr>
<tr><td style="padding:14px 26px;background:#f8fafc;border-top:1px solid #e5e7eb;color:#64748b;font-size:11px;line-height:17px">Service {esc(deployment.get('service_name'))} | Commit {esc(deployment.get('git_commit_sha'))} | Report generated directly by Railway. This report does not authorize, issue, modify, or close trades.</td></tr>
</table></td></tr></table></body></html>'''
