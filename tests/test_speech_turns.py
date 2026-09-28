from seriesai.services.speech_turns import SpeechTurnService


def test_group_segments_splits_long_gaps_and_long_turns():
    segments = [
        {"start_seconds": 0, "end_seconds": 2},
        {"start_seconds": 2.2, "end_seconds": 4},
        {"start_seconds": 5, "end_seconds": 7},
        {"start_seconds": 7.1, "end_seconds": 25.5},
    ]
    assert SpeechTurnService.group_segments(segments) == [(0.0, 4.0), (5.0, 7.0), (7.1, 25.5)]
