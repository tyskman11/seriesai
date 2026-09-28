from seriesai.services.continuity_checker import ContinuityChecker


def test_continuity_checker_reports_unknown_character_and_duration():
    document = {
        "target_duration_seconds": 600,
        "context_snapshot": {"characters": [{"character_id": "babe_001"}]},
        "outline": {"scenes": [{"sequence_number": 1, "character_ids": ["unknown_001"], "location": "Studio", "purpose": "Test", "duration_seconds": 20}]},
    }
    rules = {issue["rule"] for issue in ContinuityChecker.check(document)}
    assert {"unknown_character", "duration_mismatch"} <= rules
