import base64

from app.trading_dashboard import dashboard_authorized, render_dashboard


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
        "sources": {"coinbase": {"ok": True}, "forex": {"ok": True},
                    "solana": {"ok": True}, "discovery": {"ok": True}},
    })
    assert "Primus Trading Command Center" in body
    assert "TEST" in body
    assert "simulated" in body.lower()
    assert "cannot issue, modify, or close trades" in body

