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
    five = report.get("five_streak") or {}
    five_perf = five.get("performance") or {}
    five_promotion = five.get("promotion_checkpoint") or {}
    five_trades = five.get("trades") or []
    broker_trades = report.get("broker_open_trades") or []
    checkpoints = report.get("trade_checkpoints") or []
    learning = report.get("cross_strategy_learning") or {}
    learning_observations = learning.get("live_forex_observations") or []
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

    def rows(values: list[dict], columns: list[tuple[str, str]], *, background: str | None = None) -> str:
        if not values:
            return '<tr><td colspan="9" style="padding:12px;color:#64748b">No records</td></tr>'
        rendered = []
        for index, value in enumerate(values):
            cells = "".join(f'<td style="padding:10px;border-bottom:1px solid #e2e8f0">{html.escape(str(value.get(key, "")))}</td>' for key, _ in columns)
            row_background = background or ("#f8fafc" if index % 2 else "#ffffff")
            rendered.append(f'<tr style="background:{row_background}">{cells}</tr>')
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
                    "risk": _money(x.get("maximum_loss_usd")), "pnl": _money(x.get("realized_pnl_usd")),
                    "exit": x.get("close_reason") or "Open / broker reason unavailable",
                    "mfe": _money(x.get("max_favorable_pnl_usd")), "mae": _money(x.get("max_adverse_pnl_usd")),
                    "reason": x.get("entry_reason") or "Historical trade - entry rationale was not stored"} for x in intents]
    event_rows = [{"time": x.get("recorded_at", ""), "type": x.get("type", ""),
                   "hash": str(x.get("record_hash", ""))[:12]} for x in events]
    five_rows = [{"signal": x.get("signal_time", ""), "pair": x.get("symbol", ""),
                  "side": x.get("side", ""), "status": x.get("status", ""),
                  "entry": x.get("entry_price", ""), "stop": x.get("stop_price", ""),
                  "target": x.get("target_price", ""), "risk": _money(x.get("maximum_loss_usd")),
                  "risk_return": _pct((float(x.get("realized_pnl_usd") or 0) /
                                        float(x.get("maximum_loss_usd") or 1)) * 100),
                  "scaled_10": _money((float(x.get("realized_pnl_usd") or 0) /
                                        float(x.get("maximum_loss_usd") or 1)) * 10),
                  "exit": x.get("close_reason", ""), "pnl": _money(x.get("realized_pnl_usd")),
                  "reason": x.get("entry_reason") or "Historical trade - entry rationale was not stored"}
                 for x in five_trades]
    five_outcome_rows = [{"pair": x.get("symbol", ""), "status": x.get("status", ""),
                          "side": x.get("side", ""), "signal": x.get("signal_time", ""),
                          "reason": x.get("reason", "")}
                         for x in (five.get("outcomes") or [])]
    broker_rows = [{"id": x.get("trade_id"), "pair": x.get("instrument"), "side": x.get("side"),
                    "units": x.get("units"), "entry": x.get("entry_price"),
                    "current": x.get("current_price"),
                    "stop": x.get("stop_price"), "target": x.get("target_price"),
                    "amount": _money(x.get("notional_exposure_usd")),
                    "margin": _money(x.get("estimated_margin_allocation_usd")),
                    "risk": _money(x.get("maximum_planned_loss_usd")),
                    "pnl": _money(x.get("unrealized_pnl_usd")),
                    "return": _pct(x.get("return_on_notional_pct")),
                    "financing": _money(x.get("financing_usd"))}
                   for x in broker_trades]
    checkpoint_rows = [{"id": str(x.get("intent_id", ""))[:10], "minute": f"{x.get('checkpoint_minutes')}m",
                        "time": x.get("observed_at"), "price": x.get("executable_price"),
                        "pnl": _money(x.get("pnl_usd")), "source": x.get("source_observed_at")}
                       for x in checkpoints]
    learning_rows = [{"id": x.get("trade_id"), "pair": x.get("instrument"),
                      "current": f'{float(x.get("current_r") or 0):+.2f}R',
                      "peak": f'{float(x.get("maximum_favorable_r") or 0):+.2f}R',
                      "floor": f'{float(x.get("protected_floor_r") or 0):+.2f}R',
                      "action": "WOULD EXIT" if x.get("would_exit_now") else "MONITOR"}
                     for x in learning_observations if x.get("eligible")]

    return f'''<!doctype html><html><body style="margin:0;background:#eef2ff;font-family:Arial,sans-serif;color:#0f172a">
<div style="max-width:920px;margin:0 auto;padding:16px">
<div style="background:#172554;color:white;border-radius:16px;padding:24px"><div style="font-size:12px;opacity:.8">FOREX INTELLIGENCE • {html.escape(stamp)}</div><h1 style="margin:8px 0">Production Forex Dashboard</h1><div style="font-size:16px;margin:4px 0 14px">Live Control Strategy + Bryne and Lot-Bill Paper Experiment</div><span style="display:inline-block;background:{status_color};padding:8px 14px;border-radius:999px;font-weight:bold">{mode} • {"READY" if ready else "NOT READY"}</span></div>
<div style="display:flex;flex-wrap:wrap;gap:10px;margin:14px 0"><span style="background:#e0f2fe;color:#075985;border:1px solid #38bdf8;border-radius:999px;padding:7px 12px;font-weight:bold">BLUE • Live Control Strategy</span><span style="background:#f3e8ff;color:#6b21a8;border:1px solid #c084fc;border-radius:999px;padding:7px 12px;font-weight:bold">PURPLE • Bryne and Lot-Bill Strategy (Paper)</span></div>
<div style="display:flex;flex-wrap:wrap;gap:12px;margin:16px 0">
<div style="flex:1;min-width:180px;background:white;padding:16px;border-radius:12px"><small>Account NAV</small><div style="font-size:24px;font-weight:bold">{_money(nav)}</div></div>
<div style="flex:1;min-width:180px;background:white;padding:16px;border-radius:12px"><small>P&amp;L vs baseline</small><div style="font-size:24px;font-weight:bold;color:{'#16a34a' if pnl >= 0 else '#dc2626'}">{_money(pnl)}</div></div>
<div style="flex:1;min-width:180px;background:white;padding:16px;border-radius:12px"><small>Daily NAV change</small><div style="font-size:24px;font-weight:bold;color:{'#16a34a' if daily_pnl >= 0 else '#dc2626'}">{_money(daily_pnl)}</div></div>
<div style="flex:1;min-width:180px;background:white;padding:16px;border-radius:12px"><small>Margin used</small><div style="font-size:24px;font-weight:bold">{_money(margin)}</div></div>
<div style="flex:1;min-width:180px;background:white;padding:16px;border-radius:12px"><small>Calendar coverage</small><div style="font-size:24px;font-weight:bold">{calendar_ok}/{len(snapshots)}</div></div>
</div>
<div style="background:white;border-radius:12px;padding:18px;margin-bottom:14px"><h2>Risk and readiness</h2><p><b>Execution:</b> {"Enabled by all independent gates" if mode == "LIVE_ARMED" else "No live order authorization"}. <b>Open broker trades:</b> {int(broker.get("open_trade_count") or report.get("open_trade_count") or 0)}. <b>Pending orders:</b> {pending}. <b>Margin available:</b> {_money(broker.get("margin_available"))}. <b>Realized P&amp;L:</b> {_money(broker.get("realized_pl", report.get("realized_pnl_usd")))}. <b>Unrealized P&amp;L:</b> {_money(broker.get("unrealized_pl"))}. <b>Financing:</b> {_money(broker.get("financing"))}. <b>Last scan:</b> {html.escape(str(report.get("last_scan") or "never"))}.</p><p style="color:#b91c1c"><b>Current error:</b> {html.escape(str(report.get("last_error") or "None"))}</p></div>
<div style="background:white;border-radius:12px;padding:18px;margin-bottom:14px"><h2>Effective guardrails</h2><p><b>Minimum score:</b> {html.escape(str(risk.get("minimum_score", "N/A")))}. <b>Position limit:</b> {html.escape(str(risk.get("maximum_open_positions", "N/A")))}. <b>Risk per trade:</b> {_money(risk.get("maximum_risk_per_trade_usd"))}. <b>Open / maximum combined risk:</b> {_money(risk.get("current_open_risk_usd"))} / {_money(risk.get("maximum_combined_risk_usd"))}. <b>Daily loss limit:</b> {_money(risk.get("daily_loss_limit_usd"))}. <b>Notional limit:</b> {_money(risk.get("maximum_notional_usd"))}. <b>Margin limit:</b> {_money(risk.get("maximum_margin_used_usd"))}. <b>Currency-overlap guard:</b> {"ON" if risk.get("currency_overlap_guard") else "OFF"}.</p></div>
<div style="background:{'#fff7ed' if int(risk.get('grandfathered_positions_above_limit') or 0) else '#f0fdf4'};border:1px solid {'#fdba74' if int(risk.get('grandfathered_positions_above_limit') or 0) else '#86efac'};border-radius:12px;padding:14px;margin-bottom:14px"><b>Position-limit reconciliation:</b> {html.escape(str(risk.get('grandfathered_positions_above_limit', 0)))} protected pre-change position(s) are grandfathered for supervision and exit only. New entries remain blocked until the broker count is below the one-position limit.</div>
<div style="background:#e0f2fe;border:2px solid #38bdf8;border-radius:12px;padding:18px;margin-bottom:14px;overflow-x:auto"><h2 style="color:#075985">Current live broker positions</h2><p><b>Amount put</b> is notional market exposure. Because Forex is leveraged, estimated margin and maximum planned loss are separate.</p><table style="border-collapse:collapse;width:100%;font-size:13px"><tr><th>Trade ID</th><th>Pair</th><th>Side</th><th>Units</th><th>Entry</th><th>Executable now</th><th>Amount put / exposure</th><th>Est. margin</th><th>Max risk</th><th>Stop</th><th>Target</th><th>Unrealized</th><th>Return on amount</th><th>Financing</th></tr>{rows(broker_rows, [("id","ID"),("pair","Pair"),("side","Side"),("units","Units"),("entry","Entry"),("current","Current"),("amount","Amount"),("margin","Margin"),("risk","Risk"),("stop","Stop"),("target","Target"),("pnl","P&L"),("return","Return"),("financing","Financing")], background="#f0f9ff")}</table></div>
<div style="background:#f5f3ff;border:2px solid #8b5cf6;border-radius:12px;padding:18px;margin-bottom:14px;overflow-x:auto"><h2 style="color:#6d28d9">Cross-strategy learning • Bryne protection transfer</h2><p><b>Mode:</b> SHADOW ONLY. This measures how the Bryne profit ratchet would affect live Forex positions but cannot alter, close, or replace an OANDA order. Promotion requires 30–50 independent closed observations with positive cost-stressed expectancy.</p><table style="border-collapse:collapse;width:100%;font-size:13px"><tr><th>Trade ID</th><th>Pair</th><th>Current</th><th>Peak</th><th>Protected floor</th><th>Shadow action</th></tr>{rows(learning_rows, [("id","ID"),("pair","Pair"),("current","Current"),("peak","Peak"),("floor","Floor"),("action","Action")], background="#faf5ff")}</table></div>
<div style="background:white;border-radius:12px;padding:18px;margin-bottom:14px;overflow-x:auto"><h2>Market and calendar</h2><table style="border-collapse:collapse;width:100%;font-size:13px"><tr><th>Pair</th><th>Price</th><th>1h</th><th>24h</th><th>Spread</th><th>Calendar</th></tr>{rows(snapshot_rows, [("symbol","Pair"),("price","Price"),("1h","1h"),("24h","24h"),("spread","Spread"),("calendar","Calendar")])}</table></div>
<div style="background:white;border-radius:12px;padding:18px;margin-bottom:14px;overflow-x:auto"><h2>Latest decisions</h2><table style="border-collapse:collapse;width:100%;font-size:13px"><tr><th>Pair</th><th>Action</th><th>Score / minimum</th><th>Reason or signal ID</th></tr>{rows(outcome_rows, [("symbol","Pair"),("status","Action"),("score","Score"),("reason","Reason")])}</table></div>
<div style="background:#f3e8ff;border:2px solid #c084fc;border-radius:12px;padding:18px;margin-bottom:14px"><h2 style="color:#6b21a8">Bryne and Lot-Bill Strategy • {html.escape(str(five.get("version") or "Five-Streak"))}</h2><p><b>Status:</b> {"Enabled" if five.get("enabled") else "Disabled"}. <b>Mode:</b> PAPER ONLY. <b>Timeframe:</b> {html.escape(str(five.get("timeframe") or "M5"))}. <b>Opened:</b> {int(five_perf.get("opened") or 0)}. <b>Open:</b> {int(five_perf.get("open") or 0)}. <b>Closed:</b> {int(five_perf.get("closed") or 0)}. <b>Wins / losses:</b> {int(five_perf.get("wins") or 0)} / {int(five_perf.get("losses") or 0)}. <b>Win rate:</b> {_pct(float(five_perf.get("win_rate") or 0) * 100)}. <b>Realized P&amp;L:</b> {_money(five_perf.get("net_pnl_usd"))}. <b>Open unrealized P&amp;L:</b> {_money(five_perf.get("unrealized_pnl_usd"))}. <b>Expectancy:</b> {_money(five_perf.get("expectancy_usd"))}.</p><div style="padding:12px;background:#ede9fe;border-radius:9px"><b>Prospective promotion checkpoint:</b> {int(five_promotion.get("profitable_closes_observed") or 0)} / {int(five_promotion.get("required_additional_profitable_closes") or 2)} profitable closes. Net P&amp;L must remain positive. <b>Eligible for live review:</b> {"YES" if five_promotion.get("eligible_for_live_review") else "NO"}. Historical V2/V3 trades are excluded.</div><p>{html.escape(str(five.get("cost_model") or ""))}. This challenger cannot submit broker orders and will not promote itself automatically.</p><h3>Recent strategy evaluations</h3><div style="overflow-x:auto"><table style="border-collapse:collapse;width:100%;font-size:13px"><tr><th>Pair</th><th>Status</th><th>Side</th><th>Signal UTC</th><th>Explanation</th></tr>{rows(five_outcome_rows, [("pair","Pair"),("status","Status"),("side","Side"),("signal","Signal"),("reason","Reason")], background="#faf5ff")}</table></div><h3>Paper trades</h3><div style="overflow-x:auto"><table style="border-collapse:collapse;width:100%;font-size:13px"><tr><th>Signal UTC</th><th>Pair</th><th>Side</th><th>Status</th><th>Entry</th><th>Stop</th><th>Target</th><th>Risk</th><th>Main reason for entry</th><th>Exit</th><th>P&amp;L</th></tr>{rows(five_rows, [("signal","Signal"),("pair","Pair"),("side","Side"),("status","Status"),("entry","Entry"),("stop","Stop"),("target","Target"),("risk","Risk"),("reason","Entry reason"),("exit","Exit"),("pnl","P&L")], background="#faf5ff")}</table></div></div>
<div style="background:#e0f2fe;border:2px solid #38bdf8;border-radius:12px;padding:18px;margin-bottom:14px;overflow-x:auto"><h2 style="color:#075985">Live Control Strategy • Auditable positions and intents</h2><table style="border-collapse:collapse;width:100%;font-size:13px"><tr><th>Created</th><th>Pair</th><th>Side</th><th>Mode</th><th>Status</th><th>Score</th><th>Entry</th><th>Stop</th><th>Target</th><th>Max loss</th><th>Main reason for entry</th><th>Exit reason</th><th>MFE</th><th>MAE</th><th>Realized P&amp;L</th></tr>{rows(intent_rows, [("created","Created"),("symbol","Pair"),("side","Side"),("mode","Mode"),("status","Status"),("score","Score"),("entry","Entry"),("stop","Stop"),("target","Target"),("risk","Max loss"),("reason","Entry reason"),("exit","Exit reason"),("mfe","MFE"),("mae","MAE"),("pnl","P&amp;L")], background="#f0f9ff")}</table></div>
<div style="background:white;border-radius:12px;padding:18px;margin-bottom:14px;overflow-x:auto"><h2>Executable-price counterfactual checkpoints</h2><table style="border-collapse:collapse;width:100%;font-size:13px"><tr><th>Intent</th><th>Checkpoint</th><th>Recorded</th><th>Executable price</th><th>P&amp;L</th><th>Source time</th></tr>{rows(checkpoint_rows, [("id","Intent"),("minute","Checkpoint"),("time","Recorded"),("price","Price"),("pnl","P&L"),("source","Source")])}</table></div>
<div style="background:white;border-radius:12px;padding:18px;margin-bottom:14px;overflow-x:auto"><h2>Append-only audit trail</h2><table style="border-collapse:collapse;width:100%;font-size:13px"><tr><th>Time</th><th>Event</th><th>Record hash</th></tr>{rows(event_rows, [("time","Time"),("type","Event"),("hash","Hash")])}</table></div>
<div style="background:white;border-radius:12px;padding:18px;margin-bottom:14px"><h2>Model quality and guarded improvement</h2><p><b>Version:</b> {html.escape(str(review.get("model_version") or "FOREX_TREND_1.1"))}. <b>Closed sample:</b> {int(review.get("sample_size") or 0)}. <b>Wins/losses:</b> {int(review.get("wins") or 0)}/{int(review.get("losses") or 0)}. <b>Net expectancy:</b> {_money(review.get("net_expectancy_usd"))}. <b>Profit factor:</b> {html.escape(str(review.get("profit_factor") if review.get("profit_factor") is not None else "N/A"))}.</p><p><b>Status:</b> {html.escape(str(review.get("status") or "MODEL LOCKED - COLLECTING EVIDENCE"))}. Parameters are never changed from a handful of trades. A challenger becomes eligible only after the documented sample and prospective-performance gates pass.</p></div>
<div style="background:#fffbeb;border:1px solid #f59e0b;border-radius:12px;padding:16px"><b>Risk notice</b><p style="margin-bottom:0">No strategy can guarantee profit. Live Forex trading can lose money rapidly. Broker balances, fills, stops, financing charges and open trades must be independently verified.</p></div>
</div></body></html>'''
