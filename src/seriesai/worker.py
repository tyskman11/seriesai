"""Portable remote/local render worker; the coordinator remains DB authority."""
from __future__ import annotations

import argparse
import json
import socket
import time
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .backends.comfyui import ComfyUIWorkflowBackend
from .config import load_config
from .logging_config import configure_logging, get_logger

log = get_logger("worker")


def request_json(url: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    request = Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST" if data else "GET")
    with urlopen(request, timeout=30) as response: return json.loads(response.read())


def upload(url: str, worker_id: str, path: Path) -> dict:
    target = urlsplit(url)
    if target.scheme != "http": raise ValueError("Worker upload supports only the local coordinator HTTP URL")
    connection = HTTPConnection(target.hostname, target.port or 80, timeout=3600)
    route = target.path + (("?" + target.query) if target.query else "")
    connection.putrequest("POST", route)
    connection.putheader("Content-Type", "application/octet-stream")
    connection.putheader("Content-Length", str(path.stat().st_size))
    connection.putheader("X-SeriesAI-Worker", worker_id); connection.endheaders()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            connection.send(chunk)
    response = connection.getresponse(); body = response.read(); connection.close()
    if response.status >= 300: raise RuntimeError(f"Coordinator upload rejected ({response.status}): {body.decode(errors='replace')}")
    return json.loads(body)


INTENSITY_PAUSE_SECONDS = {"eco": 20.0, "balanced": 5.0, "full": 0.0}


def run(master_url: str, worker_id: str, config_path: Path, poll_seconds: float, intensity: str = "full") -> None:
    config = load_config(config_path)
    if config.models.video_backend != "comfyui":
        raise SystemExit("This worker requires models.video_backend: comfyui in its local config.yaml.")
    backend = ComfyUIWorkflowBackend(config.comfyui_api, config.paths.comfyui, config.paths.project_root)
    base = master_url.rstrip("/")
    intensity_pause = INTENSITY_PAUSE_SECONDS.get(intensity, 0.0)
    log.info(f"{worker_id} wartet auf lokale ComfyUI-Shots von {base} (Arbeitsintensitaet: {intensity})")
    while True:
        try:
            status = request_json(base + "/status")
            node = next((x for x in status.get("worker_nodes", []) if x.get("id") == worker_id), None)
            if node and node.get("role") == "coordinator-only":
                log.info(f"Rolle coordinator-only: Rendering pausiert."); time.sleep(max(5, poll_seconds)); continue
            inflight = request_json(base + "/render-queue/worker/" + worker_id + "/inflight").get("item")
            if inflight:
                item_id, spec = inflight["item_id"], inflight["specification"]
                submission = spec.get("backend_submission") or {}
                prompt_id = submission.get("prompt_id") if submission.get("backend") == "comfyui" else None
                if not prompt_id:
                    # Do not claim a second shot. This item predates prompt-id
                    # persistence or was interrupted before submission; a
                    # coordinator recovery will return it to retry safely.
                    log.info(f"Wartet auf Wiederherstellung von reserviertem Shot {spec.get('sequence_number')}.")
                    time.sleep(max(5, poll_seconds)); continue
                log.info(f"Setze lokalen ComfyUI-Auftrag für Shot {spec.get('sequence_number')} fort …")
                try:
                    local_output = Path(backend.wait_for_prompt(prompt_id, spec))
                    result = upload(base + "/render-queue/items/" + item_id + "/upload", worker_id, local_output)
                    log.info(f"Wiederhergestellter Shot fertig: {result.get('output_path', local_output)}")
                except Exception as error:
                    request_json(base + "/render-queue/items/" + item_id + "/fail", {"worker_id": worker_id, "error": str(error), "retry": True})
                    log.error(f"Wiederhergestellter Shot fehlgeschlagen: {error}")
                if intensity_pause: time.sleep(intensity_pause)
                continue
            claimed = request_json(base + "/render-queue/claim-next", {"worker_id": worker_id}).get("item")
            if not claimed:
                time.sleep(poll_seconds); continue
            item_id, spec = claimed["item_id"], claimed["specification"]
            log.info(f"Render {claimed['job_id']} / Shot {spec.get('sequence_number')} …")
            try:
                def submitted(prompt_id: str) -> None:
                    request_json(base + "/render-queue/items/" + item_id + "/submission", {"worker_id": worker_id, "backend": "comfyui", "prompt_id": prompt_id})
                local_output = Path(backend.render_shot(spec, on_submitted=submitted))
                result = upload(base + "/render-queue/items/" + item_id + "/upload", worker_id, local_output)
                log.info(f"Shot fertig: {result.get('output_path', local_output)}")
            except Exception as error:
                request_json(base + "/render-queue/items/" + item_id + "/fail", {"worker_id": worker_id, "error": str(error), "retry": True})
                log.error(f"Shot fehlgeschlagen, Retry gespeichert: {error}")
            if intensity_pause: time.sleep(intensity_pause)
        except (HTTPError, URLError, OSError, json.JSONDecodeError) as error:
            log.error(f"Koordinator/Netzwerk nicht erreichbar: {error}")
            time.sleep(max(5, poll_seconds))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one portable local ComfyUI SeriesAI worker.")
    parser.add_argument("--master", required=True, help="Coordinator URL, for example http://192.168.1.20:8765")
    parser.add_argument("--worker-id", default=(socket.gethostname().lower() + "::worker"))
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--poll-seconds", type=float, default=4.0)
    parser.add_argument("--intensity", choices=["eco", "balanced", "full"], default="full")
    args = parser.parse_args()
    configure_logging(Path(args.config).resolve().parent / "logs")
    run(args.master, args.worker_id, args.config, max(.5, args.poll_seconds), args.intensity)


if __name__ == "__main__": main()
