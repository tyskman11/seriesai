import pytest

from seriesai import cli_jobs


def test_normalize_nas_path_keeps_selected_project_path_relative(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    selected = tmp_path / "Series_Source" / "demo.mp4"
    selected.parent.mkdir(); selected.write_bytes(b"video")
    assert cli_jobs.normalize_nas_path("Series_Source/demo.mp4") == "Series_Source/demo.mp4"


def test_normalize_nas_path_rejects_path_outside_project_share(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="local NAS project share"):
        cli_jobs.normalize_nas_path(str(tmp_path.parent / "not-on-share.mp4"))
