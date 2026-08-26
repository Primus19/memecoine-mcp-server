from __future__ import annotations

import html
from datetime import datetime, timezone


def money(value: object) -> str:
    try:return f"${float(value):,.4f}"
    except (TypeError,ValueError):return "$0.0000"


def render_meme_report(report: dict) -> str:
    portfolio=report.get("portfolio") or {};position=portfolio.get("open_position")
    controls=portfolio.get("controls") or {};review=report.get("model_review") or {}
    mode=html.escape(str(report.get("mode") or "UNKNOWN"));paused=bool(report.get("paused"))
    verdict=f"{mode} | {'PAUSED' if paused else 'AUTO EXECUTION ENABLED'}"
    position_text=(f"{html.escape(str(position.get('product_id')))} at {money(position.get('mark_value_usdc'))}, "
                   f"unrealized {money(position.get('net_unrealized_pnl_usdc'))}") if position else "No open position"
    stamp=html.escape(str(report.get("timestamp") or datetime.now(timezone.utc).isoformat()))
    events=report.get("notification_events") or []
    event_rows="".join(f"<tr><td>{html.escape(str(x.get('at','')))}</td><td>{html.escape(str(x.get('kind','')))}</td></tr>" for x in events[:20]) or "<tr><td colspan=2>No recent events</td></tr>"
    return f'''<!doctype html><html><body style="margin:0;background:#f1f5f9;font-family:Arial,sans-serif;color:#0f172a"><div style="max-width:850px;margin:auto;padding:18px"><div style="background:#111827;color:white;border-radius:16px;padding:24px"><small>MEME COIN LIVE TRADING | {stamp}</small><h1>Production Dashboard</h1><b>{verdict}</b></div><div style="display:flex;gap:12px;flex-wrap:wrap;margin:14px 0"><div style="background:white;padding:16px;border-radius:12px;flex:1"><small>Permitted capital</small><h2>{money(report.get('permitted_capital_usdc'))}</h2></div><div style="background:white;padding:16px;border-radius:12px;flex:1"><small>Realized P&amp;L</small><h2>{money(report.get('realized_pnl_usdc'))}</h2></div><div style="background:white;padding:16px;border-radius:12px;flex:1"><small>Peak drawdown</small><h2>{float(controls.get('drawdown_pct') or 0):.2f}%</h2></div></div><div style="background:white;padding:18px;border-radius:12px;margin-bottom:14px"><h2>Position and protection</h2><p>{position_text}</p><p><b>Circuit breakers:</b> {html.escape(str(controls.get('circuit_breakers') or 'None'))}</p></div><div style="background:white;padding:18px;border-radius:12px;margin-bottom:14px"><h2>Guarded learning</h2><p>Closed sample {int(review.get('sample_size') or 0)}, wins {int(review.get('wins') or 0)}, losses {int(review.get('losses') or 0)}, expectancy {money(review.get('net_expectancy_usdc'))}, profit factor {html.escape(str(review.get('profit_factor') if review.get('profit_factor') is not None else 'N/A'))}.</p><p>{html.escape(str(review.get('status') or 'MODEL LOCKED - COLLECTING EVIDENCE'))}</p></div><div style="background:white;padding:18px;border-radius:12px"><h2>Recent audit events</h2><table style="width:100%;border-collapse:collapse"><tr><th>Time</th><th>Event</th></tr>{event_rows}</table></div></div></body></html>'''
