"""Small, immutable replacement for the retired intelligence database.

This file records only conclusions supported by repeated execution evidence.
It is deliberately not an autonomous strategy tuner.
"""

from __future__ import annotations

from datetime import datetime, timezone


ARCHIVE = {
    "created_at": "2026-09-02T05:44:00+00:00",
    "location": "/app/data/intelligence_archive_2026-09-02.json.gz",
    "counts": {"observations": 112042, "checkpoints": 36850,
               "learnings": 569, "rule_evidence": 635453},
    "database_retired": True,
}

PROVEN_CONTROLS = (
    {
        "mechanism": "EXECUTABLE_SELLABILITY",
        "action": "Require a fresh full-size entry and exit quote, acceptable round-trip recovery, and bounded impact.",
        "scope": "ALL_CRYPTO",
        "status": "MANDATORY_HARD_GATE",
    },
    {
        "mechanism": "COST_AWARE_ENTRY",
        "action": "Reject entries whose predicted net edge does not materially exceed spread, fees, and expected slippage.",
        "scope": "FOREX_AND_CRYPTO",
        "status": "MANDATORY_HARD_GATE",
    },
    {
        "mechanism": "FRESH_COMPLETE_EVIDENCE",
        "action": "Fail closed on stale quotes, contradictory calendar evidence, missing safety evidence, or inconsistent identifiers.",
        "scope": "ALL_SERVICES",
        "status": "MANDATORY_HARD_GATE",
    },
    {
        "mechanism": "ENTRY_PERSISTENCE",
        "action": "Require consecutive executable observations for experimental crypto entries; chart momentum alone is insufficient.",
        "scope": "CRYPTO_RESEARCH",
        "status": "PAPER_ONLY",
    },
    {
        "mechanism": "PROFIT_RETENTION",
        "action": "Compare stop/target with 0.5R, 0.75R, and cost-aware deterioration exits after meaningful MFE.",
        "scope": "FOREX_AND_CRYPTO_RESEARCH",
        "status": "SHADOW_ONLY_UNTIL_30_TO_50_CLOSES",
    },
)

FAILED_OR_UNPROVEN = (
    {"strategy": "FOREX_CONTROL", "evidence": "3 wins / 12 losses; negative expectancy"},
    {"strategy": "BRYNE_V5", "evidence": "1 win / 3 losses; negative expectancy"},
    {"strategy": "COINBASE_MEME", "evidence": "2 wins / 3 losses; negative realized P&L"},
    {"strategy": "RUNNER_CAPTURE", "evidence": "1 win / 13 closes; negative cost-stressed expectancy"},
    {"strategy": "SOLANA_EARLY", "evidence": "25 wins / 90 closes; negative cost-stressed P&L"},
    {"strategy": "DIVINE", "evidence": "0 wins / 34 closes"},
    {"strategy": "MICROCAP_V2", "evidence": "no completed proof sample"},
)


def consolidated_report() -> dict:
    controls = list(PROVEN_CONTROLS)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "RETIRED_AND_CONSOLIDATED",
        "archive": ARCHIVE,
        "proven_controls": list(PROVEN_CONTROLS),
        "failed_or_unproven_strategies": list(FAILED_OR_UNPROVEN),
        "totals": {"observations": ARCHIVE["counts"]["observations"],
                   "strategies": len(FAILED_OR_UNPROVEN), "shadow_observations": 0,
                   "near_misses": 0, "hard_reject_controls": 0,
                   "checkpoints": ARCHIVE["counts"]["checkpoints"],
                   "learnings": ARCHIVE["counts"]["learnings"]},
        "strategies": [],
        "recent_evidence": [],
        "actionable_recommendations": [],
        "recommendation_summary": {"total": 0, "p0": 0, "p1": 0,
                                   "live_changes_authorized": 0},
        "learnings": [{"mechanism": row["mechanism"], "status": row["status"],
                       "statement": row["action"], "sample_size": 0,
                       "adoption_threshold": "Retained consolidated control",
                       "created_at": ARCHIVE["created_at"]} for row in controls],
        "promotion_policy": {
            "all_current_entry_models_blocked": True,
            "exit_rule": "30-50 independent cost-stressed closes",
            "entry_model_or_risk": "100 independent closes with positive cost-stressed expectancy",
            "automatic_promotion": False,
        },
    }
