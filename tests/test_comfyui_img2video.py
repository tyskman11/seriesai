import json

from seriesai.backends.comfyui import ComfyUIWorkflowBackend


def _backend(tmp_path, workflow_template, img2video_template=None):
    backend = object.__new__(ComfyUIWorkflowBackend)
    backend.project_root = tmp_path

    class Config:
        pass
    config = Config()
    config.workflow_template = workflow_template
    config.img2video_workflow_template = img2video_template
    config.steps = 32
    config.cfg = 4.5
    backend.config = config
    return backend


def _write_workflow(path, marker):
    path.write_text(json.dumps({"1": {"inputs": {"marker": marker, "seed": "${seriesai.seed}"}}}), encoding="utf-8")


def test_render_shot_uses_the_plain_template_without_a_reference_image(tmp_path, monkeypatch):
    plain, img2video = tmp_path / "plain.json", tmp_path / "img2video.json"
    _write_workflow(plain, "plain"); _write_workflow(img2video, "img2video")
    backend = _backend(tmp_path, plain, img2video)
    calls = {}
    def fake_post(endpoint, payload):
        calls["workflow"] = payload["prompt"]
        return {"prompt_id": "p1"}
    monkeypatch.setattr(backend, "_post", fake_post)
    monkeypatch.setattr(backend, "wait_for_prompt", lambda prompt_id, spec: "out.mp4")
    result = backend.render_shot({"seed": 1})
    assert result == "out.mp4"
    assert calls["workflow"]["1"]["inputs"]["marker"] == "plain"


def test_render_shot_uploads_the_reference_image_and_uses_the_img2video_template(tmp_path, monkeypatch):
    plain, img2video = tmp_path / "plain.json", tmp_path / "img2video.json"
    _write_workflow(plain, "plain")
    img2video.write_text(json.dumps({"1": {"inputs": {"image": "${seriesai.image_filename}", "seed": "${seriesai.seed}"}}}), encoding="utf-8")
    backend = _backend(tmp_path, plain, img2video)
    (tmp_path / "ref.jpg").write_bytes(b"fake-jpeg")
    calls = {}
    def fake_upload(path):
        calls["uploaded"] = path
        return "ref_uploaded.jpg"
    monkeypatch.setattr(backend, "_upload_image", fake_upload)
    def fake_post(endpoint, payload):
        calls["workflow"] = payload["prompt"]
        return {"prompt_id": "p1"}
    monkeypatch.setattr(backend, "_post", fake_post)
    monkeypatch.setattr(backend, "wait_for_prompt", lambda prompt_id, spec: "out.mp4")
    result = backend.render_shot({"seed": 1, "reference_image": "ref.jpg"})
    assert result == "out.mp4"
    assert calls["uploaded"] == tmp_path / "ref.jpg"
    assert calls["workflow"]["1"]["inputs"]["image"] == "ref_uploaded.jpg"


def test_render_shot_injects_a_lora_loader_when_a_trained_lora_is_attached(tmp_path, monkeypatch):
    plain = tmp_path / "plain.json"
    plain.write_text(json.dumps({
        "1": {"class_type": "UnetLoaderGGUF", "inputs": {"seed": "${seriesai.seed}"}},
        "11": {"class_type": "CFGGuider", "inputs": {"model": ["1", 0], "positive": ["4", 0], "negative": ["5", 0], "cfg": "${seriesai.cfg}"}},
    }), encoding="utf-8")
    backend = _backend(tmp_path, plain)
    calls = {}
    def fake_post(endpoint, payload):
        calls["workflow"] = payload["prompt"]
        return {"prompt_id": "p1"}
    monkeypatch.setattr(backend, "_post", fake_post)
    monkeypatch.setattr(backend, "wait_for_prompt", lambda prompt_id, spec: "out.mp4")
    backend.render_shot({"seed": 1, "lora_name": "hudson_001_video.safetensors"})
    workflow = calls["workflow"]
    assert workflow["11"]["inputs"]["model"] == ["_lora_loader", 0]
    assert workflow["_lora_loader"]["inputs"] == {"model": ["1", 0], "lora_name": "hudson_001_video.safetensors", "strength_model": 1.0}
    assert workflow["11"]["inputs"]["cfg"] == 4.5   # other inputs untouched


def test_render_shot_falls_back_to_plain_template_when_img2video_not_configured(tmp_path, monkeypatch):
    plain = tmp_path / "plain.json"
    _write_workflow(plain, "plain")
    backend = _backend(tmp_path, plain, img2video_template=None)
    calls = {}
    def fake_post(endpoint, payload):
        calls["workflow"] = payload["prompt"]
        return {"prompt_id": "p1"}
    monkeypatch.setattr(backend, "_post", fake_post)
    monkeypatch.setattr(backend, "wait_for_prompt", lambda prompt_id, spec: "out.mp4")
    backend.render_shot({"seed": 1, "reference_image": "ref.jpg"})
    assert calls["workflow"]["1"]["inputs"]["marker"] == "plain"
