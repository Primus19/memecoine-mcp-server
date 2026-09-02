from app.consolidated_learnings import consolidated_report


def test_consolidated_report_blocks_unproven_models_and_preserves_controls():
    report = consolidated_report()
    assert report["status"] == "RETIRED_AND_CONSOLIDATED"
    assert report["archive"]["counts"]["learnings"] == 569
    assert report["promotion_policy"]["all_current_entry_models_blocked"] is True
    assert any(row["mechanism"] == "EXECUTABLE_SELLABILITY"
               for row in report["proven_controls"])
    assert all("strategy" in row and "evidence" in row
               for row in report["failed_or_unproven_strategies"])
