import base64

from unittest.mock import patch

from app.trading_dashboard import (_position_rows, build_snapshot, dashboard_authorized,
                                   render_dashboard)


def test_dashboard_basic_auth_reuses_rest_token(monkeypatch):
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    monkeypatch.setenv("REST_API_TOKEN", "dashboard-secret")
    token = base64.b64encode(b"primus:dashboard-secret").decode()
    assert dashboard_authorized("Basic " + token)
    assert not dashboard_authorized("Basic " + base64.b64encode(b"primus:wrong").decode())


def test_dashboard_renders_confirmed_and_paper_state():
    body = render_dashboard({
        "generated_at": "2026-08-28T00:00:00+00:00",
        "coinbase": {"mode": "LIVE_ARMED", "realized_pnl_usdc": -1.2,
                     "portfolio": {"controls": {"equity_usdc": 23.8, "recovery_mode": "DEFENSIVE"}}},
        "forex": {"mode": "LIVE_ARMED", "broker": {"NAV": 49.7, "realized_pl": -0.2}},
        "solana": {"paperRealizedPnlUsd": -4.0, "paperObservations": 20,
                   "paperPositions": [], "balances": {"usdc": 17, "sol": .04}, "limits": {"live": {"entryUsd": 3}}},
        "discovery": {"candidates": [{"symbol": "TEST", "score": 88}]},
        "multi_asset": {"last_scan": "2026-08-28T00:00:00+00:00",
                        "held_position_monitor": {"status": "READY", "fresh_quote_count": 1},
                        "worker_state": {"cycle_count": 4, "persistence_configured": True},
                        "multi_week_crypto": {"daily_realized_pnl_usd": 0,
                            "open_positions": [{"strategy": "MULTI_WEEK_CRYPTO_MOMENTUM_V1",
                                "symbol": "RUN", "quantity": 100, "entry_price": .50,
                                "entry_value_usd": 50, "current_mark_price": .55,
                                "current_value_usd": 55, "current_unrealized_pnl_usd": 5,
                                "return_pct": 10, "monitoring_status": "FRESH"}]}},
        "sources": {"coinbase": {"ok": True}, "forex": {"ok": True},
                    "solana": {"ok": True}, "discovery": {"ok": True},
                    "multi_week": {"ok": True}},
    })
    assert "Primus Trading Command Center" in body
    assert "TEST" in body
    assert "simulated" in body.lower()
    assert "cannot issue, modify, or close trades" in body
    assert "Market Scanner" in body
    assert "Model Performance" in body
    assert "Multi-week crypto" in body
    assert "RUN" in body
    assert "Multi-week open P&amp;L" not in body  # rendered client-side from USD data
    assert "Intelligence Ledger" in body
    assert "Evidence-backed tips and hypotheses" in body
    assert "setInterval(refresh,15*1000)" in body
    assert 'window.__INITIAL__={"generated_at"' in body
    assert "fetch('/dashboard/data'" in body


def test_dashboard_escapes_initial_json_script_breakout():
    body = render_dashboard({"generated_at": "now", "discovery": {
        "candidates": [{"symbol": "</script><script>alert(1)</script>"}]}})
    assert "</script><script>alert(1)</script>" not in body
    assert "\\u003c/script>" in body


def test_snapshot_fetches_multi_week_worker_as_first_class_source():
    upstreams = [
        ({"mode": "PAPER_ONLY"}, "forex", ""),
        ({"ok": True}, "solana", ""),
        ({"ok": True}, "discovery", ""),
        ({"multi_week_crypto": {"open_positions": []}}, "multi-week", ""),
    ]
    with patch("app.trading_dashboard.fetch_first", side_effect=upstreams) as fetch:
        snapshot = build_snapshot(lambda: {"mode": "DRY_RUN"})
    assert fetch.call_count == 4
    assert snapshot["sources"]["multi_week"]["ok"] is True
    assert snapshot["multi_asset"]["multi_week_crypto"]["open_positions"] == []


def test_server_position_table_keeps_coinbase_position_to_seven_columns():
    rendered = _position_rows({
        "coinbase": {"portfolio": {"open_position": {
            "product_id": "DOGE-USD", "status": "OPEN", "net_qty": "10",
            "entry_price": "0.10", "mark_price": "0.11",
            "net_unrealized_pnl_usdc": 0.10,
        }}},
    })

    assert rendered.count("<td>") + rendered.count("<td style=") == 7
    assert "DOGE-USD" in rendered
