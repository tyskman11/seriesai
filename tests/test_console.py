from seriesai.console import ProgressReporter


def test_progress_reporter_shows_percent_and_eta(capsys):
    reporter = ProgressReporter("TEST", 4)
    reporter.update(1, "S01E01", "completed")
    output = capsys.readouterr().out
    assert "25.0%" in output and "ETA" in output and "S01E01" in output
