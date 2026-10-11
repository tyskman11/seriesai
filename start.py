#!/usr/bin/env python3
"""The single portable SeriesAI start script.

Double-clicking this `.py` file opens exactly one visible terminal monitor on
Windows. Running `python start.py` from any terminal uses that terminal.  The
project is local-only and never needs a batch/PowerShell/admin launcher.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import re
import hashlib
import shutil
import threading
import zipfile
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent

# Keine globalen Verzeichnisse: Caches und Temp-Dateien liegen im Projekt (werden an alle Unterprozesse vererbt).
_LOCAL = ROOT / "runtime" / "cache"
for _name, _sub in (("HF_HOME", "huggingface"), ("TORCH_HOME", "torch"), ("TTS_HOME", "tts"), ("XDG_CACHE_HOME", "xdg"),
                    ("PIP_CACHE_DIR", "pip"), ("TMP", "tmp"), ("TEMP", "tmp"), ("TMPDIR", "tmp")):
    (_LOCAL / _sub).mkdir(parents=True, exist_ok=True)
    os.environ[_name] = str(_LOCAL / _sub)
VENV = ROOT / ".venv"
PORT = 8765
DISCOVERY_PORT = 8766


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def pip_args() -> list[str]:
    return ["--break-system-packages"] if sys.platform.startswith("linux") else []


def bootstrap() -> None:
    python = venv_python()
    if not python.exists():
        print("[Vorbereitung] Erstelle die lokale Python-Umgebung ...", flush=True)
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
        print("[Vorbereitung] Installation wird beim ersten Analysejob automatisch vervollstaendigt.", flush=True)

    # Never re-launch Python here.  A .py double click must retain its one
    # visible console.  The coordinator itself only uses the standard library;
    # analysis commands continue to use .venv\\Scripts\\python.exe directly.
    source = str(ROOT / "src")
    if source not in sys.path:
        sys.path.insert(0, source)
    os.environ["PYTHONPATH"] = source + os.pathsep + os.environ.get("PYTHONPATH", "")


def request_json(url: str, payload: dict | None = None) -> dict | None:
    try:
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST" if data else "GET")
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read())
    except (URLError, TimeoutError, json.JSONDecodeError):
        return None


def configured_ollama_model() -> str | None:
    """Read the small Ollama section without requiring a second Python environment."""
    match = re.search(r"(?ms)^ollama:\s*(.*?)(?=^[A-Za-z_]+:|\Z)", (ROOT / "config.yaml").read_text(encoding="utf-8"))
    if not match:
        return None
    model = re.search(r'^\s*model:\s*["\']?([^"\'\s#]+)', match.group(1), re.MULTILINE)
    return model.group(1) if model else None


def configured_video_backend() -> str | None:
    match = re.search(r"(?ms)^models:\s*(.*?)(?=^[A-Za-z_]+:|\Z)", (ROOT / "config.yaml").read_text(encoding="utf-8"))
    if not match: return None
    backend = re.search(r"^\s*video_backend:\s*[\"']?([^\"'\s#]+)", match.group(1), re.MULTILINE)
    return backend.group(1) if backend else None


def configured_work_intensity() -> str:
    match = re.search(r"(?ms)^hardware:\s*(.*?)(?=^[A-Za-z_]+:|\Z)", (ROOT / "config.yaml").read_text(encoding="utf-8"))
    if not match: return "full"
    value = re.search(r"^\s*work_intensity:\s*[\"']?([a-z]+)[\"']?", match.group(1), re.MULTILINE)
    intensity = value.group(1) if value else "full"
    return intensity if intensity in {"eco", "balanced", "full"} else "full"


def configured_gpu_vram_gb() -> int:
    match = re.search(r"(?ms)^hardware:\s*(.*?)(?=^[A-Za-z_]+:|\Z)", (ROOT / "config.yaml").read_text(encoding="utf-8"))
    value = re.search(r"^\s*gpu_vram_gb:\s*(\d+)", match.group(1), re.MULTILINE) if match else None
    return int(value.group(1)) if value else 6


def comfyui_args_for_intensity(intensity: str) -> list[str]:
    base = ["--windows-standalone-build", "--disable-auto-launch", "--enable-manager"]
    # --normalvram keeps whole model blocks resident; on a small card (the 22B LTX needs far more
    # than 6-8 GB) that fails to start or OOMs, so small GPUs always stay on --lowvram.
    if configured_gpu_vram_gb() <= 12 and intensity == "full": return base + ["--lowvram", "--reserve-vram", "0.5"]
    # eco: leave the machine usable for other things while it renders. full: use the GPU as
    # hard as this card allows. balanced keeps today's long-standing default behaviour.
    if intensity == "eco": return base + ["--lowvram", "--reserve-vram", "2"]
    if intensity == "full": return base + ["--normalvram", "--reserve-vram", "0.5"]
    return base + ["--lowvram", "--reserve-vram", "1"]


def _worker_state_path(client_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", client_id)
    return ROOT / "runtime" / f"render_worker_{safe}.json"


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, errors="replace", timeout=5)
            return str(pid) in result.stdout
        except (OSError, subprocess.SubprocessError):
            return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, SystemError):
        return False


class Supervisor:
    """Runs every SeriesAI service as a child of this one terminal.

    Each child's output is prefixed with its name (``[ComfyUI]``, ``[Worker]`` ...)
    and interleaved with the live monitor, so no extra console windows are needed.
    Strg+C stops the monitor *and* all children started here.
    """

    def __init__(self) -> None:
        self.children: dict[str, subprocess.Popen] = {}
        self.specs: dict[str, tuple[list[str], Path]] = {}

    def spawn(self, name: str, command: list[str], cwd: Path, extra_env: dict[str, str] | None = None) -> subprocess.Popen:
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", **(extra_env or {})}
        kwargs: dict = {"cwd": cwd, "env": env, "stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT}
        # Own process group: Ctrl+C is handled here and children are stopped cleanly.
        if os.name == "nt": kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else: kwargs["start_new_session"] = True
        process = subprocess.Popen(command, **kwargs)
        self.children[name], self.specs[name] = process, (command, cwd)
        threading.Thread(target=self._pump, args=(name, process), daemon=True).start()
        print(f"[{name}] gestartet (PID {process.pid}).", flush=True)
        return process

    @staticmethod
    def _pump(name: str, process: subprocess.Popen) -> None:
        for raw in iter(process.stdout.readline, b""):
            text = raw.decode("utf-8", "replace").rstrip()
            if text: print(f"[{name}] {text}", flush=True)

    def check(self, url: str) -> None:
        """Report exited children; restart the coordinator/worker unless a new coordinator took over."""
        for name, process in list(self.children.items()):
            if process.poll() is None: continue
            del self.children[name]
            print(f"[{name}] beendet (Code {process.returncode}).", flush=True)
            if name == "Koordinator":
                time.sleep(2)  # a dashboard restart re-executes the coordinator as a new process
                if request_json(f"{url}/status"): print("[Koordinator] laeuft nach Neustart weiter (Ausgabe in logs/).", flush=True)
                else: self.spawn(name, *self.specs[name])
            elif name == "Worker":
                time.sleep(5); self.spawn(name, *self.specs[name])

    def stop_all(self) -> None:
        for process in self.children.values():
            if process.poll() is None: process.terminate()
        deadline = time.monotonic() + 8
        for process in self.children.values():
            try: process.wait(max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired: process.kill()
        self.children.clear()


SUPERVISOR = Supervisor()
COMFYUI_URL = "http://127.0.0.1:8188"
COMFYUI_WATCHDOG = False   # enabled by main() once this terminal owns the local render stack


def configured_comfyui_root() -> Path | None:
    """The portable ComfyUI folder (parent of ``ComfyUI``) from ``paths.comfyui``."""
    match = re.search(r"(?ms)^paths:\s*(.*?)(?=^[A-Za-z_]+:|\Z)", (ROOT / "config.yaml").read_text(encoding="utf-8"))
    value = re.search(r"^\s*comfyui:\s*[\"']?([^\"'#\r\n]+?)[\"']?\s*(?:#.*)?$", match.group(1), re.MULTILINE) if match else None
    return (ROOT / value.group(1)).resolve().parent if value else None


COMFYUI_RELEASE_API = "https://api.github.com/repos/comfyanonymous/ComfyUI/releases/latest"
COMFYUI_ASSET = "ComfyUI_windows_portable_nvidia.7z"
COMFYUI_CUSTOM_NODES = [("ComfyUI-GGUF", "https://github.com/city96/ComfyUI-GGUF"), ("ComfyUI-KJNodes", "https://github.com/kijai/ComfyUI-KJNodes")]


def _pip_install(python: Path, *packages: str) -> bool:
    return subprocess.run([str(python), "-m", "pip", "install", *packages], cwd=ROOT).returncode == 0


def ensure_comfyui_install() -> None:
    """Install the portable ComfyUI (and its video custom nodes/models) at ``paths.comfyui`` when missing.

    Everything lands at the project-relative configured location; each step is skipped once done.
    """
    if configured_video_backend() != "comfyui": return
    root = configured_comfyui_root()
    if not root or os.name != "nt": return
    python = root / "python_embeded" / "python.exe"
    if not python.exists():
        print(f"[ComfyUI] Nicht gefunden; installiere die portable Version nach {root} ...", flush=True)
        release = request_json(COMFYUI_RELEASE_API)
        asset = next((a for a in (release or {}).get("assets", []) if a.get("name") == COMFYUI_ASSET), None)
        if not asset or not str(asset.get("digest", "")).startswith("sha256:"):
            print("[ComfyUI] Release-Informationen nicht abrufbar; spaeter erneut versuchen.", flush=True); return
        archive = ROOT / "runtime" / "downloads" / COMFYUI_ASSET
        if not download_verified(asset["browser_download_url"], archive, int(asset["size"]), asset["digest"][7:], "ComfyUI"):
            print("[ComfyUI] Download fehlgeschlagen; naechster Start setzt fort.", flush=True); return
        if not _modules_available(["py7zr"]) and not _pip_install(venv_python(), "py7zr"):
            print("[ComfyUI] py7zr konnte nicht installiert werden.", flush=True); return
        print("[ComfyUI] Entpacke (dauert einige Minuten) ...", flush=True)
        staging = root.with_name(root.name + ".staging")
        shutil.rmtree(staging, ignore_errors=True); staging.mkdir(parents=True)
        code = "import py7zr,sys;py7zr.SevenZipFile(sys.argv[1]).extractall(sys.argv[2])"
        if subprocess.run([str(venv_python()), "-c", code, str(archive), str(staging)]).returncode != 0:
            print("[ComfyUI] Entpacken fehlgeschlagen; Archiv wird neu geladen.", flush=True); archive.unlink(missing_ok=True); return
        inner = staging / "ComfyUI_windows_portable"
        shutil.rmtree(root, ignore_errors=True); (inner if inner.exists() else staging).replace(root); shutil.rmtree(staging, ignore_errors=True)
        archive.unlink(missing_ok=True)
        if not python.exists(): print("[ComfyUI] Installation unvollstaendig.", flush=True); return
    nodes = root / "ComfyUI" / "custom_nodes"
    for name, url in COMFYUI_CUSTOM_NODES:
        if (nodes / name).exists(): continue
        print(f"[ComfyUI] Installiere Custom Node {name} ...", flush=True)
        if subprocess.run(["git", "clone", "--depth", "1", url, str(nodes / name)]).returncode != 0:
            print(f"[ComfyUI] {name} konnte nicht geklont werden (Git/Netz?).", flush=True); continue
        requirements = nodes / name / "requirements.txt"
        if requirements.exists(): _pip_install(python, "-r", str(requirements))
    if not any((root / "ComfyUI" / "models" / "unet").glob("*.gguf")):
        print("[ComfyUI] Lade Videomodelle (app.py) ...", flush=True)
        subprocess.run([str(venv_python()), str(ROOT / "app.py")], cwd=ROOT)


def ensure_comfyui() -> None:
    """Start the local ComfyUI server inside this terminal unless it already answers."""
    if configured_video_backend() != "comfyui": return
    if request_json(f"{COMFYUI_URL}/system_stats"):
        print("[ComfyUI] laeuft bereits.", flush=True); return
    root = configured_comfyui_root()
    python = root / "python_embeded" / "python.exe" if root else None
    if not python or not python.exists():
        print("[ComfyUI] python_embeded nicht gefunden; bitte ComfyUI manuell starten.", flush=True); return
    intensity = configured_work_intensity()
    print(f"[ComfyUI] Arbeitsintensitaet: {intensity}", flush=True)
    SUPERVISOR.spawn("ComfyUI", [str(python), "-s", "ComfyUI/main.py", *comfyui_args_for_intensity(intensity)], root)


def comfyui_watchdog() -> None:
    """Called every monitor tick: start ComfyUI only if it neither answers nor is already being
    started by this terminal (it takes a while to answer after spawn)."""
    if configured_video_backend() != "comfyui": return
    child = SUPERVISOR.children.get("ComfyUI")
    if child is not None and child.poll() is None: return
    if request_json(f"{COMFYUI_URL}/system_stats"): return
    print("[ComfyUI] laeuft nicht - starte es.", flush=True)
    ensure_comfyui()


def ensure_local_render_worker(master_url: str, client_id: str) -> None:
    """Start exactly one local render worker in this terminal when ComfyUI rendering is enabled."""
    if configured_video_backend() != "comfyui":
        return
    state = _worker_state_path(client_id)
    try:
        saved = json.loads(state.read_text(encoding="utf-8"))
        if _pid_is_alive(int(saved.get("pid", 0))):
            print(f"[Worker] Lokaler Render-Worker laeuft bereits (PID {saved['pid']}).", flush=True)
            return
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    state.parent.mkdir(parents=True, exist_ok=True)
    worker = SUPERVISOR.spawn("Worker", [str(venv_python()), "-m", "seriesai.worker", "--master", master_url, "--worker-id", client_id, "--intensity", configured_work_intensity()], ROOT)
    state.write_text(json.dumps({"pid": worker.pid, "worker_id": client_id, "master_url": master_url}, indent=2), encoding="utf-8")


OLLAMA_HOME = ROOT / "runtime" / "ollama"
OLLAMA_MODELS = ROOT / "models" / "ollama"
OLLAMA_RELEASE_API = "https://api.github.com/repos/ollama/ollama/releases/latest"
OLLAMA_ASSET = "ollama-windows-amd64.zip"


def configured_ollama_url() -> str:
    match = re.search(r"(?ms)^ollama:\s*(.*?)(?=^[A-Za-z_]+:|\Z)", (ROOT / "config.yaml").read_text(encoding="utf-8"))
    url = re.search(r'^\s*base_url:\s*["\']?([^"\'\s#]+)', match.group(1), re.MULTILINE) if match else None
    return url.group(1).rstrip("/") if url else "http://127.0.0.1:11435"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""): digest.update(block)
    return digest.hexdigest()


def download_verified(url: str, target: Path, size: int, sha256: str | None, label: str, verify: bool = True) -> bool:
    """Download with HTTP Range resume; keep the .part file until size and SHA-256 match."""
    part = target.with_name(target.name + ".part")
    target.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, 4):
        have = part.stat().st_size if part.exists() else 0
        if have > size: part.unlink(); have = 0
        try:
            if have < size:
                request = Request(url, headers={"Range": f"bytes={have}-"} if have else {})
                with urlopen(request, timeout=60) as response:
                    if have and response.status != 206: part.unlink(); have = 0   # server ignored Range: restart
                    last = -1
                    with part.open("ab" if have else "wb") as out:
                        while chunk := response.read(1 << 20):
                            out.write(chunk); have += len(chunk)
                            if have * 100 // size != last:
                                last = have * 100 // size; print(f"[{label}] Download {last}% ({have >> 20}/{size >> 20} MB)", flush=True)
        except OSError as error:
            print(f"[{label}] Download unterbrochen ({error}); Versuch {attempt}/3 setzt fort.", flush=True); time.sleep(3); continue
        if part.stat().st_size == size and (not verify or _sha256(part) == sha256):
            part.replace(target); return True
        print(f"[{label}] Groesse/Pruefsumme stimmt nicht; lade erneut.", flush=True); part.unlink(missing_ok=True)
    return False


def ollama_executable() -> Path | None:
    """The project-local Ollama; downloaded and verified into runtime/ollama when missing."""
    exe = OLLAMA_HOME / ("ollama.exe" if os.name == "nt" else "ollama")
    if exe.exists(): return exe
    if os.name != "nt":
        found = shutil.which("ollama")
        return Path(found) if found else None
    print("[Ollama] Lokale Installation fehlt; lade sie nach runtime/ollama ...", flush=True)
    release = request_json(OLLAMA_RELEASE_API)
    asset = next((a for a in (release or {}).get("assets", []) if a.get("name") == OLLAMA_ASSET), None)
    if not asset or not str(asset.get("digest", "")).startswith("sha256:"):
        print("[Ollama] Release-Informationen nicht abrufbar; spaeter erneut versuchen.", flush=True); return None
    archive = ROOT / "runtime" / "downloads" / OLLAMA_ASSET
    if not download_verified(asset["browser_download_url"], archive, int(asset["size"]), asset["digest"][7:], "Ollama"):
        print("[Ollama] Download fehlgeschlagen; naechster Start setzt fort.", flush=True); return None
    print("[Ollama] Entpacke ...", flush=True)
    staging = OLLAMA_HOME.with_name("ollama.staging")
    shutil.rmtree(staging, ignore_errors=True)
    with zipfile.ZipFile(archive) as bundle:
        if bundle.testzip() is not None: archive.unlink(); print("[Ollama] Archiv defekt; wird neu geladen.", flush=True); return None
        bundle.extractall(staging)
    shutil.rmtree(OLLAMA_HOME, ignore_errors=True); staging.replace(OLLAMA_HOME)
    (OLLAMA_HOME / "VERSION").write_text(str(release.get("tag_name", "")), encoding="utf-8")
    archive.unlink(missing_ok=True)
    return exe if exe.exists() else None


def _model_answers(base_url: str, model: str) -> bool:
    """Smoke test: the model must load from disk and generate text."""
    try:
        request = Request(f"{base_url}/api/generate", data=json.dumps({"model": model, "prompt": "Sage: Hallo", "stream": False, "think": False, "options": {"num_predict": 8}}).encode(), headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=900) as response: return bool(json.loads(response.read()).get("response", "").strip())
    except (OSError, json.JSONDecodeError): return False


def _pull_model(base_url: str, model: str) -> bool:
    request = Request(f"{base_url}/api/pull", data=json.dumps({"name": model, "stream": True}).encode(), headers={"Content-Type": "application/json"})
    last = ""
    try:
        with urlopen(request, timeout=3600) as response:
            for raw in response:
                update = json.loads(raw)
                if update.get("error"): print(f"[Modelle] Fehler: {update['error']}", flush=True); return False
                total, completed = update.get("total"), update.get("completed")
                line = f"{model}: {completed * 100 // total}%" if total and completed is not None else update.get("status", "")
                if line and line != last: print(f"[Modelle] {line}", flush=True); last = line
        return True
    except (OSError, json.JSONDecodeError) as error:
        print(f"[Modelle] Download unterbrochen: {error}", flush=True); return False


def ensure_ollama_model() -> None:
    """Run the project-local Ollama and make sure the configured model is complete and usable."""
    model, base_url = configured_ollama_model(), configured_ollama_url()
    if not model:
        print("[Modelle] Kein Ollama-Modell konfiguriert; semantische Analyse bleibt deaktiviert.", flush=True); return
    if request_json(f"{base_url}/api/tags") is None:
        executable = ollama_executable()
        if not executable:
            print("[Modelle] Ollama ist nicht verfuegbar; Modell-Check uebersprungen.", flush=True); return
        OLLAMA_MODELS.mkdir(parents=True, exist_ok=True)
        SUPERVISOR.spawn("Ollama", [str(executable), "serve"], ROOT, {"OLLAMA_MODELS": str(OLLAMA_MODELS), "OLLAMA_HOST": base_url.split("//", 1)[-1]})
        for _ in range(60):
            time.sleep(.5)
            if request_json(f"{base_url}/api/tags") is not None: break
        else:
            print("[Modelle] Ollama-Dienst konnte nicht gestartet werden.", flush=True); return
    for attempt in range(1, 4):
        available = {item.get("name") for item in (request_json(f"{base_url}/api/tags") or {}).get("models", [])}
        if model in available or f"{model}:latest" in available:
            print(f"[Modelle] Pruefe {model} (Ladetest) ...", flush=True)
            if _model_answers(base_url, model):
                print(f"[Modelle] Ollama-Modell bereit: {model} (in {OLLAMA_MODELS})", flush=True); return
            print(f"[Modelle] {model} antwortet nicht; Pull prueft und ergaenzt fehlende Teile.", flush=True)
        else:
            print(f"[Modelle] Lade {model} nach {OLLAMA_MODELS} (Versuch {attempt}/3) ...", flush=True)
        _pull_model(base_url, model)   # Ollama resumes partial blobs and verifies every digest
    print(f"[Modelle] {model} konnte nicht vollstaendig bereitgestellt werden; naechster Start versucht es erneut.", flush=True)


TORCH_INDEX = "https://download.pytorch.org/whl/cu124"
# (label, modules that must import, pip arguments).  Order matters: torch before the TTS stack.
PYTHON_STACK = [
    ("Basis", ["pydantic", "yaml", "faster_whisper", "scenedetect", "cv2"], ["pydantic>=2.7,<3", "PyYAML>=6.0,<7", "scenedetect[opencv]>=0.6.4,<0.7", "faster-whisper>=1.1,<2"]),
    ("PyTorch", ["torch", "torchaudio"], ["torch==2.6.0", "torchaudio==2.6.0", "--index-url", TORCH_INDEX]),
    ("Sprache", ["TTS", "speechbrain", "soundfile", "sklearn"], ["coqui-tts", "transformers>=4.50,<5", "speechbrain", "soundfile", "scikit-learn"]),
]
# Model files fetched straight into the project (size and SHA-256 come from the Hugging Face API).
HF_MODELS = [
    # dvae.pth is only needed for voice_training.py's XTTS fine-tuning, not plain zero-shot cloning.
    ("XTTS-v2", "coqui/XTTS-v2", ["config.json", "vocab.json", "speakers_xtts.pth", "mel_stats.pth", "model.pth", "dvae.pth"], ROOT / "models" / "tts" / "xtts_v2"),
    ("ECAPA", "speechbrain/spkrec-ecapa-voxceleb", ["hyperparams.yaml", "embedding_model.ckpt", "mean_var_norm_emb.ckpt", "classifier.ckpt", "label_encoder.txt"], ROOT / "models" / "speechbrain" / "spkrec-ecapa-voxceleb"),
]


def _modules_available(modules: list[str]) -> bool:
    code = "import importlib.util,sys;sys.exit(0 if all(importlib.util.find_spec(m) for m in sys.argv[1:]) else 1)"
    return subprocess.run([str(venv_python()), "-c", code, *modules], capture_output=True).returncode == 0


def ensure_python_packages() -> None:
    """Install every Python dependency into the project .venv (each step retried, pip resumes from cache)."""
    for label, modules, pip in PYTHON_STACK:
        if _modules_available(modules): continue
        for attempt in range(1, 4):
            print(f"[Pip] Installiere {label} (Versuch {attempt}/3) ...", flush=True)
            process = subprocess.Popen([str(venv_python()), "-m", "pip", "install", *pip_args(), *pip], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            for raw in process.stdout:
                text = raw.decode("utf-8", "replace").rstrip()
                if text.startswith(("Downloading", "Installing", "Successfully", "ERROR")): print(f"[Pip] {text[:160]}", flush=True)
            if process.wait() == 0 and _modules_available(modules): break
        else:
            print(f"[Pip] {label} konnte nicht installiert werden; naechster Start versucht es erneut.", flush=True)


def ensure_model_files() -> None:
    """Download and verify the TTS and speaker-embedding models into models/."""
    for label, repo, files, target in HF_MODELS:
        info = request_json(f"https://huggingface.co/api/models/{repo}?blobs=true")
        remote = {item["rfilename"]: item for item in (info or {}).get("siblings", [])}
        for name in files:
            item, path = remote.get(name), target / name
            if not item:
                if not path.exists(): print(f"[Modelle] {label}: {name} nicht abrufbar (offline?).", flush=True)
                continue
            size, sha = int(item["size"]), (item.get("lfs") or {}).get("sha256")
            if path.exists() and path.stat().st_size == size and (sha is None or _sha256(path) == sha): continue
            path.unlink(missing_ok=True)
            print(f"[Modelle] {label}: lade {name} ({size >> 20} MB) ...", flush=True)
            if not download_verified(f"https://huggingface.co/{repo}/resolve/main/{name}", path, size, sha, label, verify=bool(sha)):
                print(f"[Modelle] {label}: {name} fehlgeschlagen; naechster Start setzt fort.", flush=True)
        else:
            print(f"[Modelle] {label} geprueft: {target}", flush=True)
    ensure_face_detector_model()
    ensure_face_recognizer_model()


# opencv-python 5.x dropped cv2.CascadeClassifier/cv2.data.haarcascades; character-reference-image
# face detection uses this small YuNet ONNX model instead (verified opencv_zoo release asset).
YUNET_URL = "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
YUNET_SIZE, YUNET_SHA256 = 232589, "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
YUNET_PATH = ROOT / "models" / "opencv" / "face_detection_yunet_2023mar.onnx"


def ensure_face_detector_model() -> None:
    if YUNET_PATH.is_file() and YUNET_PATH.stat().st_size == YUNET_SIZE and _sha256(YUNET_PATH) == YUNET_SHA256:
        print(f"[Modelle] Gesichtserkennung (YuNet) geprueft: {YUNET_PATH}", flush=True); return
    YUNET_PATH.unlink(missing_ok=True)
    print("[Modelle] Gesichtserkennung (YuNet): lade face_detection_yunet_2023mar.onnx (227 KB) ...", flush=True)
    if not download_verified(YUNET_URL, YUNET_PATH, YUNET_SIZE, YUNET_SHA256, "YuNet"):
        print("[Modelle] Gesichtserkennung (YuNet): Download fehlgeschlagen; naechster Start setzt fort.", flush=True)


# SFace tells two detected faces apart (an embedding, not just "a face is here" like YuNet).
# Used to find every frame of a specific character across a whole series automatically
# for LoRA training data, instead of trusting a single hand-picked evidence frame.
SFACE_URL = "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx"
SFACE_SIZE, SFACE_SHA256 = 38696353, "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"
SFACE_PATH = ROOT / "models" / "opencv" / "face_recognition_sface_2021dec.onnx"


def ensure_face_recognizer_model() -> None:
    if SFACE_PATH.is_file() and SFACE_PATH.stat().st_size == SFACE_SIZE and _sha256(SFACE_PATH) == SFACE_SHA256:
        print(f"[Modelle] Gesichtswiedererkennung (SFace) geprueft: {SFACE_PATH}", flush=True); return
    SFACE_PATH.unlink(missing_ok=True)
    print("[Modelle] Gesichtswiedererkennung (SFace): lade face_recognition_sface_2021dec.onnx (37 MB) ...", flush=True)
    if not download_verified(SFACE_URL, SFACE_PATH, SFACE_SIZE, SFACE_SHA256, "SFace"):
        print("[Modelle] Gesichtswiedererkennung (SFace): Download fehlgeschlagen; naechster Start setzt fort.", flush=True)


def _discover_once(port: int, timeout: float) -> str | None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1); sock.settimeout(timeout)
        try:
            sock.sendto(b"SERIESAI_DISCOVER_V1", ("255.255.255.255", DISCOVERY_PORT))
            while True:
                response = json.loads(sock.recvfrom(2048)[0])
                if response.get("service") == "seriesai" and response.get("port") == port: return response["url"]
        except (OSError, TimeoutError, json.JSONDecodeError):
            return None


def discover_coordinator(port: int, attempts: int = 3, timeout: float = 1.5) -> str | None:
    # A single short broadcast window is exactly the race where two machines starting at the
    # same moment can both find nobody home and both decide to become their own coordinator.
    # A few retries shrink that window a lot without needing real distributed consensus.
    for attempt in range(attempts):
        found = _discover_once(port, timeout)
        if found: return found
        if attempt < attempts - 1: time.sleep(0.3)
    return None


def configured_role() -> str:
    """Top-level `role: auto|worker-only` in config.yaml. 'worker-only' means this machine
    must never self-elect as coordinator, no matter the discovery timing - the reliable way
    to guarantee "client 2 and beyond are always workers" regardless of startup order."""
    config_path = ROOT / "config.yaml"
    if not config_path.is_file(): return "auto"
    match = re.search(r'^\s*role:\s*["\']?([a-z-]+)["\']?\s*(?:#.*)?$', config_path.read_text(encoding="utf-8"), re.MULTILINE)
    role = match.group(1) if match else "auto"
    return role if role in {"auto", "worker-only"} else "auto"


def human_duration(seconds: int | None) -> str:
    if seconds is None: return "wird berechnet"
    hours, remainder = divmod(max(0, seconds), 3600)
    return f"{hours} h {remainder // 60:02d} min"


def monitor(url: str, interval: float, client_id: str, mode: str) -> None:
    links = f"Dashboard: {url}/" + (f"  |  ComfyUI: {COMFYUI_URL}/" if configured_video_backend() == "comfyui" else "")
    print(f"\n{links}")
    print(f"Dieser Rechner: {mode}")
    print("Kein Fenster oeffnet sich automatisch; die Links oben bleiben stehen. Live-Monitor aktiv, alle Dienste laufen in diesem Terminal. Strg+C beendet sie alle.\n")
    restart_generation: int | None = None
    try:
        while True:
            SUPERVISOR.check(url)
            if COMFYUI_WATCHDOG: comfyui_watchdog()
            status = request_json(f"{url}/status"); stamp = time.strftime("%H:%M:%S")
            if not status:
                print(f"[{stamp}] Warte auf den Koordinator ...", flush=True)
            else:
                pipeline = status.get("pipeline", {}); active = pipeline.get("active", [])
                generation = status.get("restart_generation", 0)
                if restart_generation is None:
                    restart_generation = generation
                elif generation != restart_generation:
                    print("[Neustart] Vom Master angefordert. Dieser Client startet im selben Terminal neu ...", flush=True)
                    SUPERVISOR.stop_all()
                    os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]])
                own_node = next((node for node in status.get("worker_nodes", []) if node.get("id") == client_id), None)
                own_role = own_node.get("role", "wird zugewiesen") if own_node else "nicht verbunden"
                current = ", ".join(f"{item['stage']}: {item['episode_id']}" for item in active) or "keine aktive Datenbankaufgabe"
                counts = pipeline.get("queue_counts", {}); shot = pipeline.get("current_shot") or {}
                render = f"Render {counts.get('completed', 0)}/{pipeline.get('render_total_shots', 0)}"
                if shot: render += f" · Shot {shot.get('sequence_number')} · {shot.get('worker_id', 'unbekannter Worker')}"
                print(f"[{stamp}] Eigene Rolle: {own_role} | Verbundene Clients: {status.get('workers', 0)} | Transkripte {pipeline.get('transcripts', 0)}/{pipeline.get('audio', 0)} | Szenen {pipeline.get('scenes', 0)}/{pipeline.get('episodes', 0)} | {render} | Render-ETA {human_duration(pipeline.get('render_eta_seconds'))}\n           Jetzt: {current}\n           {links}", flush=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nBeende alle SeriesAI-Dienste ...", flush=True)
        SUPERVISOR.stop_all()
        print("Alles beendet. Unterbrochene Renderjobs setzt `recover-queue` fort.")


def main() -> None:
    global COMFYUI_WATCHDOG
    if hasattr(sys.stdout, "reconfigure"): sys.stdout.reconfigure(line_buffering=True, errors="replace")
    parser = argparse.ArgumentParser(description="Start or join SeriesAI and keep the live monitor visible.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", default=PORT, type=int)
    parser.add_argument("--join", metavar="URL")
    parser.add_argument("--refresh-seconds", type=float, default=5.0)
    parser.add_argument("--no-install", action="store_true", help="Skip the Python package and model download/verification step.")
    parser.add_argument("--no-render-worker", action="store_true", help="Do not start the optional configured local render worker.")
    args = parser.parse_args()
    print("\n=== SeriesAI: lokaler Master & Live-Monitor ===\nProjekt: " + str(ROOT), flush=True)
    bootstrap()
    if not args.no_install:
        ensure_python_packages(); ensure_model_files()
    ensure_ollama_model()
    local_url = f"http://127.0.0.1:{args.port}"; status = request_json(f"{local_url}/status")
    monitor_url = local_url; client_id = "master"; mode = "Master & Koordinator"
    role = configured_role()
    if not status:
        join_url = args.join or discover_coordinator(args.port)
        if not join_url and role == "worker-only":
            print("config.yaml: role=worker-only - dieser Client wird nie selbst Koordinator, warte auf einen Master im Netzwerk ...", flush=True)
            while not join_url:
                time.sleep(2.0); join_url = discover_coordinator(args.port)
        if join_url:
            name = os.environ.get("COMPUTERNAME") or socket.gethostname()
            client_id = f"{name.lower()}::{sys.platform.lower()}"
            result = request_json(f"{join_url.rstrip('/')}/workers/register", {"name": name, "platform": sys.platform, "client_id": client_id, "work_intensity": configured_work_intensity()})
            if not result: raise SystemExit(f"Koordinator konnte diesen Worker nicht aufnehmen: {join_url}")
            print(f"Mit Master verbunden: {join_url} (Worker {result['worker_id']}).", flush=True); monitor_url = join_url.rstrip("/"); mode = f"Worker von {monitor_url}"
        else:
            # The coordinator runs as a child of this terminal (output prefixed
            # [Koordinator]) with the project venv, so its optional local modules
            # (YAML, Pydantic, future backends) are available after a restart.
            SUPERVISOR.spawn("Koordinator", [str(venv_python()), "-m", "seriesai.coordinator", "--host", args.host, "--port", str(args.port)], ROOT)
            for _ in range(20):
                time.sleep(.25); status = request_json(f"{local_url}/status")
                if status: break
            if not status: raise SystemExit("Koordinator konnte nicht gestartet werden; siehe logs/coordinator.error.log")
            print(f"SeriesAI gestartet (PID {status['pid']}).", flush=True)
    else:
        print(f"SeriesAI laeuft bereits (PID {status['pid']}, Worker {status['workers']}).", flush=True)
    if not args.no_render_worker:
        if not args.no_install: ensure_comfyui_install()
        ensure_comfyui()
        COMFYUI_WATCHDOG = True
        ensure_local_render_worker(monitor_url, client_id)
    monitor(monitor_url, max(1.0, args.refresh_seconds), client_id, mode)


if __name__ == "__main__": main()
