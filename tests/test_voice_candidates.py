from seriesai.services.voice_candidates import VoiceCandidateService


def test_select_turns_prefers_clean_duration_and_spreads_candidates():
    turns = [
        {"start_seconds": 0, "duration": 7},
        {"start_seconds": 10, "duration": 6},
        {"start_seconds": 150, "duration": 8},
        {"start_seconds": 300, "duration": 4},
        {"start_seconds": 450, "duration": 20},
    ]
    selected = VoiceCandidateService.select_turns(turns)
    assert [item["start_seconds"] for item in selected] == [0, 150, 300]
