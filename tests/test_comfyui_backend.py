from seriesai.backends.comfyui import ComfyUIWorkflowBackend


def test_workflow_placeholders_are_replaced_without_mutating_other_values():
    backend = object.__new__(ComfyUIWorkflowBackend)
    workflow = {"1": {"inputs": {"text": "${seriesai.prompt}", "seed": "${seriesai.seed}", "fixed": "keep"}}}
    assert backend._replace(workflow, {"prompt": "Hallo", "seed": 12}) == {"1": {"inputs": {"text": "Hallo", "seed": 12, "fixed": "keep"}}}


def test_unknown_workflow_placeholder_is_rejected():
    backend = object.__new__(ComfyUIWorkflowBackend)
    try:
        backend._replace("${seriesai.missing}", {})
    except ValueError as error:
        assert "unknown SeriesAI field" in str(error)
    else:
        raise AssertionError("unknown workflow placeholder was accepted")


def test_wait_for_prompt_fails_fast_when_comfyui_no_longer_knows_the_prompt(monkeypatch):
    import pytest
    from seriesai.backends.comfyui import ComfyUIWorkflowBackend

    backend = object.__new__(ComfyUIWorkflowBackend)

    class Config:
        timeout_seconds, poll_seconds = 3600, 0
    backend.config = Config()
    monkeypatch.setattr(backend, "_get", lambda endpoint: {} if endpoint.startswith("/history") else {"queue_running": [], "queue_pending": []})
    with pytest.raises(RuntimeError, match="kennt den Auftrag"):
        backend.wait_for_prompt("lost", {})


def test_wait_for_prompt_keeps_waiting_while_the_prompt_is_queued(monkeypatch):
    import pytest
    from seriesai.backends.comfyui import ComfyUIWorkflowBackend

    backend = object.__new__(ComfyUIWorkflowBackend)

    class Config:
        timeout_seconds, poll_seconds = 1, 0
    backend.config = Config()
    monkeypatch.setattr(backend, "_get", lambda endpoint: {} if endpoint.startswith("/history") else {"queue_running": [[1, "busy"]], "queue_pending": []})
    with pytest.raises(TimeoutError):
        backend.wait_for_prompt("busy", {})
