from app.multi_week_crypto import CHECKPOINT_DAYS, STRATEGY, evaluate_candidate, manage_position


def candidate(**changes):
    base = {
        "chain": "base", "contract": "0xabc", "symbol": "TEST", "observed_at": "2026-09-03T00:00:00Z",
        "token_age_days": 20, "liquidity_usd": 2_000_000, "volume_24h_usd": 3_000_000,
        "market_cap_usd": 5_000_000,
        "round_trip_recovery": .995, "sell_impact_bps": 40, "sell_route_ok": True,
        "security_verified": True, "top10_holder_fraction": .35, "creator_fraction": .02,
        "confirmation_count": 2, "confirmation_span_hours": 24,
        "price_above_20d_average": True, "daily_higher_highs": True, "daily_higher_lows": True,
        "relative_strength_7d_pct": 12, "volume_7d_vs_prior_ratio": 1.4, "holder_growth_7d_pct": 8,
        "controlled_pullback_or_consolidation": True, "extension_from_20d_fraction": .08,
    }
    return {**base, **changes}


def test_qualifies_persistent_executable_candidate_for_paper_only():
    result = evaluate_candidate(candidate())
    assert result["qualified"] is True
    assert result["decision"] == "PAPER_STAGE_1"
    assert result["strategy"] == STRATEGY
    assert result["mode"] == "PAPER_ONLY"
    assert result["checkpoint_days"] == list(CHECKPOINT_DAYS)


def test_cex_order_book_evidence_replaces_inapplicable_contract_concentration_checks():
    result = evaluate_candidate(candidate(
        chain="coinbase-spot", contract="BTC-USD", execution_evidence_mode="CEX_ORDER_BOOK",
        venue_operational=True, security_verified=False, top10_holder_fraction=None,
        creator_fraction=None, holder_growth_7d_pct=0,
    ))
    assert result["qualified"] is True
    assert "contract safety not verified" not in result["hard_gate_failures"]


def test_cex_candidate_fails_closed_when_venue_is_unavailable():
    result = evaluate_candidate(candidate(
        execution_evidence_mode="CEX_ORDER_BOOK", venue_operational=False,
        security_verified=False, top10_holder_fraction=None, creator_fraction=None,
    ))
    assert result["qualified"] is False
    assert "execution venue unavailable" in result["hard_gate_failures"]


def test_rejects_unsellable_vertical_runner_even_with_good_trend():
    result = evaluate_candidate(candidate(
        sell_route_ok=False, round_trip_recovery=.20, sell_impact_bps=9_999,
        extension_from_20d_fraction=.80, controlled_pullback_or_consolidation=False,
    ))
    assert result["qualified"] is False
    assert "full-position sell route unavailable" in result["hard_gate_failures"]
    assert "entry is extended or lacks a controlled consolidation" in result["hard_gate_failures"]


def test_requires_temporal_confirmation_not_repeated_same_scan():
    result = evaluate_candidate(candidate(confirmation_count=3, confirmation_span_hours=1))
    assert result["qualified"] is False
    assert "requires two confirmations at least 12 hours apart" in result["hard_gate_failures"]


def test_executable_emerging_candidate_enters_research_cohort_before_full_history():
    result = evaluate_candidate(candidate(
        security_verified=False, top10_holder_fraction=None, creator_fraction=None,
        confirmation_count=1, confirmation_span_hours=0,
        price_above_20d_average=False, daily_higher_highs=False, daily_higher_lows=False,
        relative_strength_7d_pct=0, volume_7d_vs_prior_ratio=0,
        holder_growth_7d_pct=0, controlled_pullback_or_consolidation=False,
    ))
    assert result["qualified"] is False
    assert result["research_eligible"] is True
    assert result["decision"] == "RESEARCH_PAPER_HOLD"


def test_established_cex_asset_does_not_consume_emerging_research_capacity():
    result = evaluate_candidate(candidate(
        chain="coinbase-spot", contract="BTC-USD", execution_evidence_mode="CEX_ORDER_BOOK",
        venue_operational=True, confirmation_count=1, confirmation_span_hours=0,
        controlled_pullback_or_consolidation=False,
    ))
    assert result["qualified"] is False
    assert result["research_eligible"] is False
    assert "established CEX asset is outside emerging research cohort" in result["research_failures"]


def test_research_cohort_never_relaxes_execution_quality():
    result = evaluate_candidate(candidate(
        security_verified=False, top10_holder_fraction=None, creator_fraction=None,
        sell_route_ok=False, round_trip_recovery=.5, sell_impact_bps=5000,
    ))
    assert result["research_eligible"] is False
    assert "full-position sell route unavailable" in result["research_failures"]


def test_research_cohort_requires_asymmetric_market_cap_headroom():
    result = evaluate_candidate(candidate(
        market_cap_usd=150_000_000, security_verified=False,
        top10_holder_fraction=None, creator_fraction=None,
    ))
    assert result["research_eligible"] is False
    assert "market cap too large for emerging upside cohort" in result["research_failures"]


def test_profit_manager_takes_partial_at_two_r():
    result = manage_position(
        {"entry_price": 100, "initial_stop_price": 90, "peak_executable_price": 121},
        {"executable_price": 120, "sell_route_ok": True, "round_trip_recovery": .99,
         "security_verified": True, "price_above_20d_average": True, "daily_higher_lows": True,
         "relative_strength_7d_pct": 5, "volume_7d_vs_prior_ratio": 1.1},
    )
    assert result["action"] == "TAKE_PROFIT"
    assert result["fraction"] == .25


def test_profit_manager_exits_on_multi_factor_deterioration():
    result = manage_position(
        {"entry_price": 100, "initial_stop_price": 90, "peak_executable_price": 115},
        {"executable_price": 108, "sell_route_ok": True, "round_trip_recovery": .99,
         "security_verified": True, "price_above_20d_average": False, "daily_higher_lows": False,
         "relative_strength_7d_pct": -2, "volume_7d_vs_prior_ratio": .7},
    )
    assert result["action"] == "EXIT"
    assert result["reason"] == "multi-factor trend deterioration"


def test_execution_failure_always_forces_exit():
    result = manage_position(
        {"entry_price": 100, "initial_stop_price": 90, "peak_executable_price": 140},
        {"executable_price": 130, "sell_route_ok": False, "round_trip_recovery": .4,
         "security_verified": True},
    )
    assert result["action"] == "EXIT"
    assert result["fraction"] == 1


def test_research_hold_does_not_exit_only_because_history_is_incomplete():
    result = manage_position(
        {"entry_price": 100, "initial_stop_price": 88, "peak_executable_price": 100,
         "research_only": True},
        {"executable_price": 99, "sell_route_ok": True, "round_trip_recovery": .98,
         "security_verified": False, "daily_candle_count": 1, "market_cap_usd": 5_000_000,
         "liquidity_usd": 1_000_000, "volume_24h_usd": 2_000_000},
    )
    assert result["action"] == "HOLD"


def test_research_hold_exits_when_asymmetric_upside_disappears():
    result = manage_position(
        {"entry_price": 100, "initial_stop_price": 88, "peak_executable_price": 100,
         "research_only": True},
        {"executable_price": 99, "sell_route_ok": True, "round_trip_recovery": .98,
         "security_verified": False, "daily_candle_count": 1, "market_cap_usd": 50_000_000,
         "liquidity_usd": 1_000_000, "volume_24h_usd": 2_000_000},
    )
    assert result["action"] == "EXIT"
    assert "asymmetry" in result["reason"]
