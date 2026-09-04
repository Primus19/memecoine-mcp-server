from unittest.mock import patch

from app.emerging_crypto import (DEFAULT_RESEARCH_SEEDS, _addresses, _best_pairs, _execution, _retained_candles,
                                 emerging_crypto_universe, refresh_held_position_quotes)


def test_default_research_seeds_always_include_user_observed_robinhood_contracts():
    with patch("app.emerging_crypto.fetch_json", return_value=[]), patch.dict(
            "os.environ", {"EMERGING_CRYPTO_SEED_CONTRACTS": ""}):
        addresses = _addresses()
    assert all(item in addresses for item in DEFAULT_RESEARCH_SEEDS)


def test_selects_deepest_pair_per_contract():
    rows = [
        {"chainId": "base", "baseToken": {"address": "0xabc"}, "liquidity": {"usd": 10}},
        {"chainId": "base", "baseToken": {"address": "0xABC"}, "liquidity": {"usd": 20}},
    ]
    result = _best_pairs(rows)
    assert len(result) == 1
    assert result[0]["liquidity"]["usd"] == 20


def test_amm_execution_model_is_cost_stressed():
    result = _execution(1_000_000, 100, 60)
    assert result["sell_route_ok"] is True
    assert .97 < result["round_trip_recovery"] < 1
    assert result["execution_evidence_mode"] == "DEX_AMM_STRESS_MODEL"


def test_held_position_refresh_returns_cost_stressed_sell_mark():
    pair = {"chainId": "robinhood", "pairAddress": "0xpool",
            "baseToken": {"address": "0xtoken", "symbol": "RUN"},
            "priceUsd": "1", "liquidity": {"usd": 500_000},
            "volume": {"h24": 1_000_000}, "marketCap": 5_000_000,
            "url": "https://dexscreener.com/robinhood/0xpool"}
    position = {"chain": "robinhood", "contract": "0xtoken", "symbol": "RUN",
                "fill_price": 1.0, "quantity": 50}
    with patch("app.emerging_crypto._pairs", return_value=[pair]):
        rows, errors = refresh_held_position_quotes([position])
    assert errors == []
    assert len(rows) == 1
    assert 0 < rows[0]["executable_sell_price"] < rows[0]["price"]
    assert rows[0]["price_source"] == "DEXSCREENER_HELD_POSITION_AMM_STRESS_MARK"


def test_dynamic_emerging_candidate_preserves_unsupported_chain_safety_failure():
    pair = {
        "chainId": "robinhood", "pairAddress": "0xpool", "pairCreatedAt": 1_784_000_000_000,
        "baseToken": {"address": "0xtoken", "symbol": "RUN", "name": "Runner"},
        "priceUsd": "1", "liquidity": {"usd": 500_000}, "volume": {"h24": 1_000_000},
        "marketCap": 5_000_000, "url": "https://dexscreener.com/robinhood/0xpool",
    }
    candles = [{"observed_at": f"2026-08-{day:02d}T00:00:00+00:00", "open": day,
                "high": day + 1, "low": day - .5, "close": day + .5,
                "volume_usd": 1_000_000} for day in range(1, 25)]
    with patch("app.emerging_crypto._addresses", return_value=[("robinhood", "0xtoken")]), \
         patch("app.emerging_crypto._pairs", return_value=[pair]), \
         patch("app.emerging_crypto._candles", return_value=candles), \
         patch("app.emerging_crypto._security", return_value={
             "security_verified": False, "security_failures": ["chain safety adapter unavailable"],
             "top10_holder_fraction": None, "creator_fraction": None, "security_source_url": "",
         }), patch("app.emerging_crypto.time.sleep"):
        rows, health = emerging_crypto_universe()
    assert health["candidate_count"] == 1
    assert rows[0]["contract"] == "0xtoken"
    assert rows[0]["security_verified"] is False
    assert "chain safety adapter unavailable" in rows[0]["security_failures"]


def test_missing_robinhood_ohlcv_still_retains_visible_forward_control():
    pair = {"chainId": "robinhood", "pairAddress": "0xpool", "pairCreatedAt": 1_784_000_000_000,
            "baseToken": {"address": "0xtoken", "symbol": "RUN", "name": "Runner"},
            "priceUsd": "1", "liquidity": {"usd": 500_000}, "volume": {"h24": 1_000_000},
            "marketCap": 5_000_000, "url": "https://dexscreener.com/robinhood/0xpool"}
    with patch("app.emerging_crypto._addresses", return_value=[("robinhood", "0xtoken")]), \
         patch("app.emerging_crypto._pairs", return_value=[pair]), \
         patch("app.emerging_crypto._candles", side_effect=RuntimeError("network unsupported")), \
         patch("app.emerging_crypto._retained_candles", return_value=[]), \
         patch("app.emerging_crypto.time.sleep"):
        rows, health = emerging_crypto_universe()
    assert len(rows) == 1
    assert rows[0]["daily_candles"] == []
    assert health["errors"][0]["error"].startswith("OHLCV")


def test_retains_one_real_observation_per_day(tmp_path, monkeypatch):
    monkeypatch.setenv("EMERGING_CRYPTO_HISTORY_PATH", str(tmp_path / "history.json"))
    first = _retained_candles("robinhood", "0xabc", 1.0, 1000)
    second = _retained_candles("robinhood", "0xabc", 1.2, 1200)
    assert len(first) == len(second) == 1
    assert second[0]["open"] == 1.0
    assert second[0]["high"] == 1.2
    assert second[0]["close"] == 1.2
