"""Local ComfyUI API adapter for one resumable SeriesAI shot at a time."""
from __future__ import annotations

import json
import shutil
import time
import uuid
from collections.abc import Callable
from pathlib import Path
import socket
import subprocess
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

    def is_running(self) -> bool:
        try:
            self._get("/system_stats"); return True
        except RuntimeError:
            return False

    @staticmethod
    def launch_args(intensity: str, gpu_vram_gb: int) -> list[str]:
        """Same VRAM policy as start.py: small GPUs always stay on --lowvram."""
        base = ["--windows-standalone-build", "--disable-auto-launch", "--enable-manager"]
        if gpu_vram_gb <= 12 and intensity == "full": return base + ["--lowvram", "--reserve-vram", "0.5"]
        if intensity == "eco": return base + ["--lowvram", "--reserve-vram", "2"]
        if intensity == "full": return base + ["--normalvram", "--reserve-vram", "0.5"]
        return base + ["--lowvram", "--reserve-vram", "1"]

    def _launch_in_progress(self) -> bool:
        """True while some ComfyUI main.py process exists (e.g. started by start.py and still booting)."""
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  "(@(Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like '*ComfyUI*main.py*' })).Count"],
                                 capture_output=True, text=True, timeout=30).stdout.strip()
            return out.isdigit() and int(out) > 0
        except (OSError, subprocess.SubprocessError):
            return False

    def ensure_running(self, intensity: str = "full", gpu_vram_gb: int = 6, log=None, timeout: float = 3600.0) -> None:
        """Start the portable ComfyUI (``<paths.comfyui>/..``) if its API does not answer, then wait for it."""
        say = log or (lambda message: None)
        if self.is_running(): return
        portable = Path(self.comfyui_root).resolve().parent
        python = portable / "python_embeded" / "python.exe"
        if not python.exists():
            raise RuntimeError(f"ComfyUI nicht startbar: {python} fehlt (python start.py installiert es)")
        if self._launch_in_progress():
            say("ComfyUI startet bereits, warte auf die API ...")
        else:
            say(f"ComfyUI nicht erreichbar - starte {portable.name} ...")
            logfile = (Path(self.project_root) / "logs" / "comfyui.log").open("ab")
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            subprocess.Popen([str(python), "-s", "ComfyUI/main.py", *self.launch_args(intensity, gpu_vram_gb)],
                             cwd=portable, stdout=logfile, stderr=subprocess.STDOUT, creationflags=flags)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.is_running(): say("ComfyUI bereit."); return
            time.sleep(3)
        raise RuntimeError(f"ComfyUI antwortet nach {int(timeout)} s nicht (siehe logs/comfyui.log)")

    def render_shot(self, specification: dict, on_submitted: Callable[[str], None] | None = None) -> str:
        template_path = self.config.workflow_template
        reference_image = specification.get("reference_image")
        if reference_image and self.config.img2video_workflow_template:
            # A real face crop of the shot's character anchors the generation, instead of pure
            # text-to-video (which cannot reproduce a specific person's likeness at all).
            template_path = self.config.img2video_workflow_template
            specification = {**specification, "image_filename": self._upload_image(Path(self.project_root) / reference_image)}
        template = json.loads(Path(template_path).read_text(encoding="utf-8"))
        client_id = str(uuid.uuid4())
        defaults = {**self.RENDER_DEFAULTS, "steps": self.config.steps, "cfg": self.config.cfg}
        workflow = self._replace(template, {**defaults, **specification})
        lora_name = specification.get("lora_name")
        if lora_name:
            workflow = self._inject_lora(workflow, lora_name)
        queued = self._post("/prompt", {"prompt": workflow, "client_id": f"seriesai-{client_id}"})
        prompt_id = queued.get("prompt_id")
        if not prompt_id:
            raise RuntimeError("ComfyUI did not return a prompt_id")
        if on_submitted: on_submitted(str(prompt_id))
        return self.wait_for_prompt(str(prompt_id), specification)

    def wait_for_prompt(self, prompt_id: str, specification: dict) -> str:
        deadline = time.monotonic() + self.config.timeout_seconds
        missing = 0
        while time.monotonic() < deadline:
            try:
                history = self._get(f"/history/{prompt_id}")
            except RuntimeError:
                # A single slow/dropped poll shouldn't abort a render that may still be
                # running for tens of minutes; just retry on the next tick.
                time.sleep(self.config.poll_seconds)
                continue
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
            elif self._prompt_gone(prompt_id):
                # Not finished, not running, not pending: ComfyUI restarted or dropped it. Waiting out the full
                # timeout would just stall the queue for hours; fail now so the shot is retried.
                missing += 1
                if missing >= 3: raise RuntimeError(f"ComfyUI kennt den Auftrag {prompt_id} nicht mehr (Neustart?) - Shot wird neu eingereicht")
            else:
                missing = 0
            time.sleep(self.config.poll_seconds)
        raise TimeoutError(f"ComfyUI did not complete shot within {self.config.timeout_seconds}s")

    # Both bundled workflow templates feed the base model straight from node "1"
    # (UnetLoaderGGUF) into node "11" (CFGGuider)'s "model" input. A trained character LoRA
    # is spliced in between those two, without needing a separate LoRA-variant template file.
    _CFGGUIDER_NODE, _BASE_MODEL_NODE = "11", "1"

    def _prompt_gone(self, prompt_id: str) -> bool:
        try:
            queue = self._get("/queue")
        except RuntimeError:
            return False   # API hiccup: not proof the prompt is lost
        return not any(len(item) > 1 and item[1] == prompt_id for key in ("queue_running", "queue_pending") for item in queue.get(key, []))

    def _inject_lora(self, workflow: dict, lora_name: str) -> dict:
        guider = workflow.get(self._CFGGUIDER_NODE)
        if not guider or guider.get("class_type") != "CFGGuider":
            raise RuntimeError(f"Workflow template has no CFGGuider at node {self._CFGGUIDER_NODE}; cannot apply a LoRA.")
        workflow = dict(workflow)
        workflow["_lora_loader"] = {"class_type": "LoraLoaderModelOnly", "inputs": {"model": [self._BASE_MODEL_NODE, 0], "lora_name": lora_name, "strength_model": 1.0}}
        workflow[self._CFGGUIDER_NODE] = {**guider, "inputs": {**guider["inputs"], "model": ["_lora_loader", 0]}}
        return workflow

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
        except (HTTPError, URLError, socket.timeout, json.JSONDecodeError) as error: raise RuntimeError(f"ComfyUI API unavailable: {error}") from error

    def _post(self, endpoint: str, payload: dict) -> dict:
        request = Request(self.base_url + endpoint, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=30) as response: return json.loads(response.read())
        except (HTTPError, URLError, json.JSONDecodeError) as error: raise RuntimeError(f"ComfyUI API request failed: {error}") from error

    def _upload_image(self, path: Path) -> str:
        """Upload a local reference image to ComfyUI's input store; returns the filename to use in a LoadImage node."""
        if not path.is_file(): raise FileNotFoundError(f"Reference image not found: {path}")
        boundary = f"seriesai-{uuid.uuid4().hex}"
        data = path.read_bytes()
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{path.name}\"\r\n"
            f"Content-Type: image/jpeg\r\n\r\n"
        ).encode() + data + f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\ntrue\r\n--{boundary}--\r\n".encode()
        request = Request(self.base_url + "/upload/image", data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urlopen(request, timeout=30) as response: result = json.loads(response.read())
        except (HTTPError, URLError, json.JSONDecodeError) as error: raise RuntimeError(f"ComfyUI image upload failed: {error}") from error
        return result["name"]
