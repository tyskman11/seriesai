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
