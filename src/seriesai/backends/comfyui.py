"""Local ComfyUI API adapter for one resumable SeriesAI shot at a time."""
from __future__ import annotations

import json
import shutil
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .base import VideoBackend
from ..config import ComfyUIConfig


# Wan's recommended negative prompt (English), plus series-specific guards.
DEFAULT_NEGATIVE_PROMPT = ("blurry, low quality, oversaturated, overexposed, static, still image, worst quality, jpeg artifacts, "
                       "ugly, deformed, disfigured, extra fingers, poorly drawn hands, poorly drawn face, extra limbs, fused fingers, "
                       "three legs, cluttered background, walking backwards, text, subtitles, watermark, logo")


class ComfyUIWorkflowBackend(VideoBackend):
    """Render via an exported *local* ComfyUI API workflow.

    Template values such as ``${seriesai.prompt}``, ``${seriesai.seed}`` and
    ``${seriesai.duration_seconds}`` are recursively replaced. The workflow is
    never edited in place.  ComfyUI output is copied to the queue's immutable
    ``output_path`` only after its API reports success.
    """
    # Low-VRAM resolution (divisible by 32 for LTX); a shot spec may override it.
    RENDER_DEFAULTS = {"width": 768, "height": 448}

    def __init__(self, config: ComfyUIConfig, comfyui_root: Path, project_root: Path) -> None:
        if not config.workflow_template:
            raise ValueError("comfyui_api.workflow_template is required for local rendering")
        self.config, self.comfyui_root, self.project_root = config, comfyui_root, project_root
        self.base_url = config.api_url.rstrip("/")
        if not self.base_url.startswith("http://127.0.0.1") and not self.base_url.startswith("http://localhost"):
            raise ValueError("ComfyUI API must be a local loopback address on the rendering worker")

    def render_shot(self, specification: dict, on_submitted: Callable[[str], None] | None = None) -> str:
        template = json.loads(Path(self.config.workflow_template).read_text(encoding="utf-8"))
        client_id = str(uuid.uuid4())
        workflow = self._replace(template, {**self.RENDER_DEFAULTS, **specification})
        queued = self._post("/prompt", {"prompt": workflow, "client_id": f"seriesai-{client_id}"})
        prompt_id = queued.get("prompt_id")
        if not prompt_id:
            raise RuntimeError("ComfyUI did not return a prompt_id")
        if on_submitted: on_submitted(str(prompt_id))
        return self.wait_for_prompt(str(prompt_id), specification)

    def wait_for_prompt(self, prompt_id: str, specification: dict) -> str:
        deadline = time.monotonic() + self.config.timeout_seconds
        while time.monotonic() < deadline:
            history = self._get(f"/history/{prompt_id}")
            # Some ComfyUI releases key history by internal prompt id. A query
            # with our id will be empty until the server has completed it.
            record = history.get(prompt_id) if isinstance(history, dict) else None
            if record:
                status = record.get("status", {})
                if status.get("status_str") == "error":
                    messages = status.get("messages", [])
                    raise RuntimeError("ComfyUI shot failed: " + json.dumps(messages, ensure_ascii=False)[-2000:])
                outputs = record.get("outputs", {})
                asset = self._first_video(outputs)
                if asset:
                    return self._copy_output(asset, specification)
            time.sleep(self.config.poll_seconds)
        raise TimeoutError(f"ComfyUI did not complete shot within {self.config.timeout_seconds}s")

    def _replace(self, value, spec):
        # Keys starting with "_" are template comments (_readme, _note) that ComfyUI rejects.
        if isinstance(value, dict): return {key: self._replace(item, spec) for key, item in value.items() if not key.startswith("_")}
        if isinstance(value, list): return [self._replace(item, spec) for item in value]
        if isinstance(value, str) and value.startswith("${seriesai.") and value.endswith("}"):
            key = value[11:-1]
            if key not in spec: raise ValueError(f"Workflow requests unknown SeriesAI field: {key}")
            return spec[key]
        return value

    @staticmethod
    def _first_video(outputs: dict) -> dict | None:
        for node_output in outputs.values():
            for key in ("gifs", "videos", "images"):
                files = node_output.get(key, []) if isinstance(node_output, dict) else []
                if files: return files[0]
        return None

    def _copy_output(self, asset: dict, spec: dict) -> str:
        filename = Path(asset.get("filename", "")).name
        subfolder = Path(asset.get("subfolder", ""))
        kind = asset.get("type", "output")
        if kind != "output": raise RuntimeError("ComfyUI returned a non-output asset")
        source = (self.comfyui_root / "output" / subfolder / filename).resolve()
        output = (self.project_root / spec["output_path"]).resolve()
        if not source.is_file() or self.comfyui_root.resolve() not in source.parents:
            raise FileNotFoundError(f"ComfyUI output not found: {source}")
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".partial")
        shutil.copy2(source, temporary); temporary.replace(output)
        return str(output)

    def _get(self, endpoint: str) -> dict:
        try:
            with urlopen(self.base_url + endpoint, timeout=20) as response: return json.loads(response.read())
        except (HTTPError, URLError, json.JSONDecodeError) as error: raise RuntimeError(f"ComfyUI API unavailable: {error}") from error

    def _post(self, endpoint: str, payload: dict) -> dict:
        request = Request(self.base_url + endpoint, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=30) as response: return json.loads(response.read())
        except (HTTPError, URLError, json.JSONDecodeError) as error: raise RuntimeError(f"ComfyUI API request failed: {error}") from error
