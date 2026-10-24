#!/usr/bin/env python3
"""The single portable SeriesAI start script.

Double-clicking this `.py` file opens exactly one visible terminal monitor on
Windows. Running `python start.py` from any terminal uses that terminal.  The
project is local-only and never needs a batch/PowerShell/admin launcher.
"""
from __future__ import annotations

import argparse
import atexit
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
INSTANCE_LOCK = ROOT / "runtime" / "seriesai.start.lock"


def _lock_owner() -> int | None:
    try:
        return int(INSTANCE_LOCK.read_text(encoding="utf-8").strip().splitlines()[0].split("=", 1)[1])
    except (OSError, IndexError, ValueError):
        return None


def _pid_running(pid: int | None) -> bool:
    if not pid or pid == os.getpid():
        return pid == os.getpid()
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _lock_started_at() -> float | None:
    try:
        values = dict(line.split("=", 1) for line in INSTANCE_LOCK.read_text(encoding="utf-8").splitlines() if "=" in line)
        return float(values.get("started", ""))
    except (OSError, ValueError):
        return None


def _dashboard_listening() -> bool:
    """A living PID alone is not a valid SeriesAI instance after a failed restart."""
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=1):
            return True
    except OSError:
        return False


def _is_our_start_process(pid: int) -> bool:
    """Only terminate a stale lock owner when Linux proves it is this launcher."""
    if os.name == "nt":
        return False
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace")
        return "start.py" in command and "SeriesAI" in command
    except OSError:
        return False


def acquire_instance_lock() -> bool:
    """Ensure a NAS restart cannot run two model downloaders at once."""
    INSTANCE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = os.open(INSTANCE_LOCK, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        owner = _lock_owner()
        # A controlled os.execv restart retains its PID and the lock file.
        # It is the same monitor, not a competing launcher.
        if owner == os.getpid():
            return True
        lock_age = time.time() - (_lock_started_at() or time.time())
        if _pid_running(owner) and _dashboard_listening():
            print(f"[Start] SeriesAI laeuft bereits (PID {owner}); zweiter Start wird beendet.", flush=True)
            return False
        if _pid_running(owner) and lock_age >= 120 and _is_our_start_process(owner):
            print(f"[Start] Hängengebliebenen SeriesAI-Start (PID {owner}, ohne Dashboard) wird beendet.", flush=True)
            try:
                os.kill(owner, 15)
            except OSError:
                pass
            for _ in range(20):
                if not _pid_running(owner):
                    break
                time.sleep(0.25)
        if _pid_running(owner):
            print(f"[Start] SeriesAI-Start (PID {owner}) antwortet nicht; Sperre bleibt zum Schutz vor Doppelstarts.", flush=True)
            return False
        INSTANCE_LOCK.unlink(missing_ok=True)
        return acquire_instance_lock()
    with os.fdopen(handle, "w", encoding="utf-8") as lock:
        lock.write(f"pid={os.getpid()}\nstarted={time.time()}\n")
    return True


def release_instance_lock() -> None:
    if _lock_owner() == os.getpid():
        INSTANCE_LOCK.unlink(missing_ok=True)

# Keine globalen Verzeichnisse: Caches und Temp-Dateien liegen im Projekt (werden an alle Unterprozesse vererbt).
_LOCAL = ROOT / "runtime" / "cache"
for _name, _sub in (("HF_HOME", "huggingface"), ("TORCH_HOME", "torch"), ("TTS_HOME", "tts"), ("XDG_CACHE_HOME", "xdg"),
                    ("PIP_CACHE_DIR", "pip"), ("TMP", "tmp"), ("TEMP", "tmp"), ("TMPDIR", "tmp")):
    (_LOCAL / _sub).mkdir(parents=True, exist_ok=True)
    os.environ[_name] = str(_LOCAL / _sub)


# ---- portabler Python (python-build-standalone): liegt im Projekt bzw. im gemeinsamen runtime-Ordner, nie in einer globalen Installation ----
PORTABLE_PY_MINOR = "3.11"      # 3.10-3.12 haben fuer alle KI-Pakete (torch, coqui-tts ...) Wheels


def portable_python() -> Path | None:
    exe = "python.exe" if os.name == "nt" else "bin/python3"
    for base in (ROOT / "runtime" / "python", ROOT.parent.parent / "runtime" / "python"):   # projekteigen, sonst gemeinsam im Pool-Ordner
        if (base / exe).exists() and (base / ".install_complete").exists(): return base / exe   # Marker: nur vollstaendig entpackte Installationen
    return None


def ensure_portable_python() -> Path | None:
    """Gibt den portablen Python zurueck; fehlt er, wird er einmalig (mit SHA-256-Pruefung) geladen. None = offline/fehlgeschlagen."""
    import hashlib, json, tarfile, urllib.request
    found = portable_python()
    if found: return found
    platform_tag = "x86_64-pc-windows-msvc" if os.name == "nt" else ("aarch64-apple-darwin" if sys.platform == "darwin" else "x86_64-unknown-linux-gnu")
    target = ROOT.parent.parent / "runtime" / "python" if ROOT.parent.name == "projects" else ROOT / "runtime" / "python"
    try:
        request = urllib.request.Request("https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest", headers={"User-Agent": "portable-python"})
        with urllib.request.urlopen(request, timeout=30) as response: release = json.load(response)
        pattern = re.compile(rf"^cpython-{re.escape(PORTABLE_PY_MINOR)}\.\d+\+\d+-{platform_tag}-install_only\.tar\.gz$")
        asset = next(a for a in release["assets"] if pattern.match(a["name"]))
        digest = str(asset.get("digest", ""))
        if not digest.startswith("sha256:"): raise RuntimeError("Release ohne Pruefsumme")
        archive = ROOT / "runtime" / "downloads" / asset["name"]
        archive.parent.mkdir(parents=True, exist_ok=True)
        print(f"[Python] Lade portables Python {PORTABLE_PY_MINOR} ({asset['size'] >> 20} MB) nach {target} ...", flush=True)
        sha = hashlib.sha256()
        with urllib.request.urlopen(asset["browser_download_url"], timeout=60) as response, archive.open("wb") as out:
            while chunk := response.read(1 << 20): out.write(chunk); sha.update(chunk)
        if sha.hexdigest() != digest[7:]: archive.unlink(missing_ok=True); raise RuntimeError("Pruefsumme stimmt nicht")
        staging = target.with_name("python.staging")
        shutil.rmtree(staging, ignore_errors=True)
        skipped = 0
        with tarfile.open(archive) as bundle:
            members = bundle.getmembers()
            for number, member in enumerate(members, 1):      # einzeln entpacken: auf Netzlaufwerken scheitert gelegentlich eine Datei (z. B. leere .lock-Dateien)
                if number % 250 == 0: print(f"[Python] Entpacke ... {number}/{len(members)} Dateien", flush=True)
                for attempt in range(3):
                    try:
                        bundle.extract(member, staging, filter="data") if hasattr(tarfile, "data_filter") else bundle.extract(member, staging)
                        break
                    except PermissionError:
                        if attempt == 2:
                            if not member.name.endswith(".lock"): skipped += 1
                            break
                        time.sleep(1)
        if skipped or not (staging / "python" / ("python.exe" if os.name == "nt" else "bin/python3")).exists():
            raise RuntimeError(f"Entpacken unvollstaendig ({skipped} Dateien nicht schreibbar; laeuft ein Virenscanner/anderer Prozess auf dem Ziel?)")
        archive.unlink(missing_ok=True)
        shutil.rmtree(target, ignore_errors=True)
        for attempt in range(5):   # Netzlaufwerke/Virenscanner halten frisch entpackte Ordner kurz fest
            try: (staging / "python").replace(target); break
            except PermissionError:
                if attempt == 4: shutil.move(str(staging / "python"), str(target))
                else: time.sleep(2)
        shutil.rmtree(staging, ignore_errors=True)
        if not portable_python():
            shutil.rmtree(target, ignore_errors=True); raise RuntimeError("python nach dem Verschieben nicht vorhanden")
        (target / ".install_complete").write_text("ok", encoding="utf-8")
        if not portable_python():
            shutil.rmtree(target, ignore_errors=True); raise RuntimeError("Marker konnte nicht geschrieben werden")
        print("[Python] Portabler Python bereit.", flush=True)
    except Exception as error:  # noqa: BLE001 - offline o. ae.: Aufrufer weicht auf den startenden Python aus
        print(f"[Python] Portabler Python nicht verfuegbar ({type(error).__name__}: {error}); verwende den startenden Python.", flush=True)
        return None
    return portable_python()
# The NAS host can create a Linux ``.venv`` in this shared project.  It cannot
# run on Windows, so never reuse or overwrite it from the Windows launcher.
VENV = ROOT / (".venv-windows" if os.name == "nt" else ".venv")
PORT = 8770          # lokales Dashboard (ASMRAI 8780, MusicAI 8790); der All-for-One-Hub ist der Koordinator


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def pip_args() -> list[str]:
    return ["--break-system-packages"] if sys.platform.startswith("linux") else []


def bootstrap() -> None:
    python = venv_python()
    if not python.exists():
        print("[Vorbereitung] Erstelle die lokale Python-Umgebung ...", flush=True)
        # A locally installed interpreter is sufficient to create the small
        # project venv.  Do not block the dashboard behind a first-run runtime
        # download; the portable runtime remains an optional fallback.
        subprocess.run([str(portable_python() or sys.executable), "-m", "venv", str(VENV)], check=True)
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


def llm_settings() -> dict[str, str]:
    """Read the flat llm: section of config.yaml without requiring a second Python environment."""
    match = re.search(r"(?ms)^llm:\s*(.*?)(?=^[A-Za-z_]+:|\Z)", (ROOT / "config.yaml").read_text(encoding="utf-8"))
    values: dict[str, str] = {}
    for key, value in re.findall(r'^\s+([a-z_]+):\s*["\']?([^"\'\s#]+)', match.group(1) if match else "", re.MULTILINE):
        values[key] = value
    return {k: v for k, v in values.items() if v.lower() not in ("null", "~")}


def flat_settings(section: str) -> dict[str, str]:
    """Read a simple top-level config section without importing the project venv."""
    text = (ROOT / "config.yaml").read_text(encoding="utf-8")
    match = re.search(rf"(?ms)^{re.escape(section)}:\s*(.*?)(?=^[A-Za-z_]+:|\Z)", text)
    values: dict[str, str] = {}
    for key, value in re.findall(r'^\s+([a-z_]+):\s*["\']?([^"\'\s#]+)', match.group(1) if match else "", re.MULTILINE):
        values[key] = value
    return {k: v for k, v in values.items() if v.lower() not in ("null", "~")}


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
    base = ["--windows-standalone-build", "--disable-auto-launch"]   # ohne --enable-manager: der Manager braucht pip/uv, das die portable Installation nicht hat (Absturz beim Start)
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
        replace_dir(inner if inner.exists() else staging, root); shutil.rmtree(staging, ignore_errors=True)
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


def comfyui_process_exists() -> bool:
    """True while any ComfyUI main.py process is alive, also one still booting (slow network drives take minutes)."""
    if os.name != "nt": return False
    command = "(@(Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like '*ComfyUI*main.py*' })).Count"
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", command], capture_output=True, text=True, timeout=30).stdout.strip()
        return out.isdigit() and int(out) > 0
    except (OSError, subprocess.SubprocessError):
        return False


def ensure_comfyui() -> None:
    """Start the local ComfyUI server inside this terminal unless it already answers."""
    if configured_video_backend() != "comfyui": return
    if request_json(f"{COMFYUI_URL}/system_stats"):
        print("[ComfyUI] laeuft bereits.", flush=True); return
    if comfyui_process_exists():
        print("[ComfyUI] ein Prozess startet bereits (z. B. durch ein anderes Projekt) - klinke mich ein, sobald er antwortet.", flush=True); return
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


LLM_HOME = ROOT / "runtime" / "llamacpp"
LLM_MODELS = ROOT / "models" / "llm"
VISION_MODELS = ROOT / "models" / "vision"
LLAMACPP_RELEASES_API = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=1"


def configured_llm_url() -> str:
    return llm_settings().get("base_url", "http://127.0.0.1:11435").rstrip("/")


def vision_settings() -> dict[str, str]:
    return flat_settings("vision")


def configured_vision_url() -> str:
    return vision_settings().get("base_url", "http://127.0.0.1:11436").rstrip("/")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""): digest.update(block)
    return digest.hexdigest()


def replace_dir(source: Path, target: Path) -> None:
    """Move a freshly extracted folder into place; on network drives or with a virus scanner holding it,
    a plain rename fails with PermissionError, so retry and finally fall back to copy + delete."""
    shutil.rmtree(target, ignore_errors=True)
    for attempt in range(5):
        try: source.replace(target); return
        except PermissionError: time.sleep(2)
    shutil.copytree(source, target, dirs_exist_ok=True); shutil.rmtree(source, ignore_errors=True)


def transfer_stats(done: int, total: int, start_bytes: int, started: float) -> str:
    """'12.3 MB/s, noch 4m 12s' from the bytes moved since this connection opened."""
    speed = (done - start_bytes) / max(time.time() - started, 1e-6)
    left = int((total - done) / speed) if speed > 0 else -1
    eta = f", noch {left // 3600}h {left % 3600 // 60:02d}m {left % 60:02d}s" if left >= 3600 else f", noch {left // 60}m {left % 60:02d}s" if left >= 0 else ""
    return f"{speed / 1e6:.1f} MB/s{eta}"


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
                    last, shown, began, began_bytes = -1, 0.0, time.time(), have
                    with part.open("ab" if have else "wb") as out:
                        while chunk := response.read(1 << 20):
                            out.write(chunk); have += len(chunk)
                            if have * 100 // size != last and time.time() - shown >= 1.0:
                                last, shown = have * 100 // size, time.time()
                                print(f"[{label}] Download {last}% ({have >> 20}/{size >> 20} MB, {transfer_stats(have, size, began_bytes, began)})", flush=True)
        except OSError as error:
            print(f"[{label}] Download unterbrochen ({error}); Versuch {attempt}/3 setzt fort.", flush=True); time.sleep(3); continue
        if part.stat().st_size == size and (not verify or _sha256(part) == sha256):
            part.replace(target); return True
        print(f"[{label}] Groesse/Pruefsumme stimmt nicht; lade erneut.", flush=True); part.unlink(missing_ok=True)
    return False


def _has_nvidia() -> bool:
    return shutil.which("nvidia-smi") is not None


def llamacpp_executable() -> Path | None:
    """The project-local llama-server; downloaded and verified into runtime/llamacpp when missing."""
    name = "llama-server.exe" if os.name == "nt" else "llama-server"
    found = next(LLM_HOME.rglob(name), None) if LLM_HOME.exists() else None
    if found: return found
    print("[LLM] Lokaler llama.cpp-Server fehlt; lade ihn nach runtime/llamacpp ...", flush=True)
    releases = request_json(LLAMACPP_RELEASES_API)
    release = releases[0] if isinstance(releases, list) and releases else (releases or {})
    names = {a["name"]: a for a in release.get("assets", [])}
    if os.name == "nt":
        wanted = [n for n in names if n.endswith("-bin-win-cuda-12.4-x64.zip") or n.startswith("cudart-llama-bin-win-cuda-12.4-x64")] if _has_nvidia() \
            else [n for n in names if n.endswith("-bin-win-cpu-x64.zip")]
    else:
        wanted = [n for n in names if n.endswith("-bin-ubuntu-x64.zip") or n.endswith("-bin-ubuntu-x64.tar.gz")]
    if not wanted:
        print("[LLM] Release-Informationen nicht abrufbar; spaeter erneut versuchen.", flush=True); return None
    staging = LLM_HOME.with_name("llamacpp.staging")
    shutil.rmtree(staging, ignore_errors=True)
    for asset_name in wanted:
        asset = names[asset_name]
        archive = ROOT / "runtime" / "downloads" / asset_name
        digest = str(asset.get("digest", ""))
        if not download_verified(asset["browser_download_url"], archive, int(asset["size"]), digest[7:] if digest.startswith("sha256:") else None, "LLM", verify=digest.startswith("sha256:")):
            print("[LLM] Download fehlgeschlagen; naechster Start setzt fort.", flush=True); return None
        print(f"[LLM] Entpacke {asset_name} ...", flush=True)
        if asset_name.endswith(".zip"):
            with zipfile.ZipFile(archive) as bundle:
                if bundle.testzip() is not None: archive.unlink(); print("[LLM] Archiv defekt; wird neu geladen.", flush=True); return None
                bundle.extractall(staging)
        else:
            import tarfile
            with tarfile.open(archive) as bundle: bundle.extractall(staging)
        archive.unlink(missing_ok=True)
    replace_dir(staging, LLM_HOME)
    (LLM_HOME / "VERSION").write_text(str(release.get("tag_name", "")), encoding="utf-8")
    found = next(LLM_HOME.rglob(name), None)
    if found and os.name != "nt": found.chmod(0o755)
    return found


def llm_model_file() -> Path | None:
    """The configured GGUF in models/llm, else the newest one there, else download the configured Hugging Face file."""
    settings = llm_settings()
    LLM_MODELS.mkdir(parents=True, exist_ok=True)
    if settings.get("model_file"):
        chosen = LLM_MODELS / settings["model_file"]
        if chosen.exists(): return chosen
        print(f"[LLM] {chosen} fehlt.", flush=True)
    else:
        ggufs = sorted(LLM_MODELS.glob("*.gguf"), key=lambda f: f.stat().st_mtime, reverse=True)
        if ggufs: return ggufs[0]
    repo, filename = settings.get("hf_repo"), settings.get("model_file") or settings.get("hf_file")
    if not repo or not filename:
        print("[LLM] Kein GGUF-Modell in models/llm und kein hf_repo/hf_file konfiguriert; LLM bleibt aus.", flush=True); return None
    tree = request_json(f"https://huggingface.co/api/models/{repo}/tree/main") or []
    entry = next((e for e in tree if isinstance(e, dict) and e.get("path") == filename), None)
    if not entry or not entry.get("lfs"):
        print(f"[LLM] {repo}/{filename} nicht abrufbar; spaeter erneut versuchen.", flush=True); return None
    print(f"[LLM] Lade Modell {filename} nach models/llm ...", flush=True)
    target = LLM_MODELS / filename
    ok = download_verified(f"https://huggingface.co/{repo}/resolve/main/{filename}", target, int(entry["lfs"]["size"]), entry["lfs"]["oid"], "LLM")
    return target if ok else None


def ensure_llm_server() -> None:
    """Run the project-local llama-server with a verified GGUF model (any GGUF in models/llm can be used)."""
    base_url = configured_llm_url()
    if request_json(f"{base_url}/health") is not None:
        print("[LLM] laeuft bereits.", flush=True); return
    model = llm_model_file()
    if not model: return
    executable = llamacpp_executable()
    if not executable:
        print("[LLM] llama-server ist nicht verfuegbar; LLM-Funktionen bleiben aus.", flush=True); return
    settings = llm_settings()
    host, _, port = base_url.split("//", 1)[-1].partition(":")
    command = [str(executable), "-m", str(model), "--host", host, "--port", port or "11435", "-c", settings.get("ctx_size", "16384"),
               "-ngl", settings.get("gpu_layers", "99"), "--jinja"]
    print(f"[LLM] Starte llama-server mit {model.name} ...", flush=True)
    SUPERVISOR.spawn("LLM", command, ROOT)
    for _ in range(600):
        time.sleep(.5)
        if request_json(f"{base_url}/health") is not None:
            print(f"[LLM] Modell bereit: {model.name} (in {LLM_MODELS})", flush=True); return
    print("[LLM] Modell wurde nicht rechtzeitig geladen; Log oben pruefen.", flush=True)


def vision_model_files() -> tuple[Path, Path] | None:
    """Fetch the VLM and projector with resumable, hash-checked downloads."""
    settings = vision_settings()
    if settings.get("enabled", "true").lower() in {"false", "0", "no"}:
        print("[Vision] In config.yaml deaktiviert.", flush=True); return None
    repo = settings.get("hf_repo")
    model_name = settings.get("model_file") or settings.get("hf_model_file")
    projector_name = settings.get("mmproj_file") or settings.get("hf_mmproj_file")
    if not repo or not model_name or not projector_name:
        print("[Vision] hf_repo, model_file oder mmproj_file fehlt in config.yaml.", flush=True); return None
    VISION_MODELS.mkdir(parents=True, exist_ok=True)
    # The NAS pool can briefly overlap old and new start.py processes during a
    # restart.  One downloader must own a .part file; Range requests from two
    # processes would otherwise corrupt it despite download_verified().
    lock = VISION_MODELS / ".download.lock"
    try:
        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        try:
            age = time.time() - lock.stat().st_mtime
            if age > 3600:
                lock.unlink(missing_ok=True)
                return vision_model_files()
        except OSError:
            pass
        print("[Vision] Download wird bereits von einem anderen SeriesAI-Prozess ausgefuehrt.", flush=True)
        return None
    os.write(descriptor, f"pid={os.getpid()} started={time.time()}\n".encode())
    os.close(descriptor)
    tree = request_json(f"https://huggingface.co/api/models/{repo}/tree/main") or []
    files: list[Path] = []
    try:
        for name, kind in ((model_name, "Vision-Modell"), (projector_name, "Vision-Projektor")):
            target = VISION_MODELS / name
            entry = next((e for e in tree if isinstance(e, dict) and e.get("path") == name), None)
            if not entry or not entry.get("lfs"):
                print(f"[Vision] {repo}/{name} nicht abrufbar; spaeter erneut versuchen.", flush=True); return None
            if not target.exists() or target.stat().st_size != int(entry["lfs"]["size"]):
                print(f"[Vision] Lade {kind} {name} nach models/vision ...", flush=True)
                if not download_verified(f"https://huggingface.co/{repo}/resolve/main/{name}", target, int(entry["lfs"]["size"]), entry["lfs"]["oid"], "Vision"):
                    return None
            files.append(target)
        return files[0], files[1]
    finally:
        lock.unlink(missing_ok=True)


def ensure_vision_server() -> None:
    """Start an isolated local VLM server without taking over the text-LLM port."""
    base_url = configured_vision_url()
    if request_json(f"{base_url}/health") is not None:
        print("[Vision] laeuft bereits.", flush=True); return
    files = vision_model_files()
    if not files: return
    model, projector = files
    executable = llamacpp_executable()
    if not executable:
        print("[Vision] llama-server ist nicht verfuegbar; Vision bleibt aus.", flush=True); return
    settings = vision_settings()
    host, _, port = base_url.split("//", 1)[-1].partition(":")
    command = [str(executable), "-m", str(model), "--mmproj", str(projector), "--host", host, "--port", port or "11436",
               "-c", settings.get("ctx_size", "4096"), "-ngl", settings.get("gpu_layers", "0"), "--jinja"]
    print(f"[Vision] Starte llama-server mit {model.name} und {projector.name} ...", flush=True)
    SUPERVISOR.spawn("Vision", command, ROOT)
    for _ in range(600):
        time.sleep(.5)
        if request_json(f"{base_url}/health") is not None:
            print(f"[Vision] Modell bereit: {model.name} (in {VISION_MODELS})", flush=True); return
    print("[Vision] Modell wurde nicht rechtzeitig geladen; Log oben pruefen.", flush=True)


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


def ensure_dashboard_packages() -> None:
    """Install only the coordinator's small dependency set before dashboard start."""
    if _modules_available(["pydantic", "yaml"]):
        return
    print("[Vorbereitung] Installiere Dashboard-Grundlagen (pydantic, PyYAML) ...", flush=True)
    if not _pip_install(venv_python(), "pydantic>=2.7,<3", "PyYAML>=6.0,<7"):
        raise RuntimeError("Dashboard-Grundlagen konnten nicht installiert werden.")


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


def human_duration(seconds: int | None) -> str:
    if seconds is None: return "wird berechnet"
    hours, remainder = divmod(max(0, seconds), 3600)
    return f"{hours} h {remainder // 60:02d} min"


def monitor(url: str, interval: float, client_id: str, mode: str, recover_server=None) -> None:
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
                # A long model download can outlive an already running
                # coordinator. Recover it here instead of leaving the public
                # dashboard at 502 while the Vision server is healthy.
                if recover_server:
                    recover_server()
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


def safe_step(label: str, step) -> None:
    """Run one setup step; a failure is reported but must never stop the remaining services (e.g. ComfyUI)."""
    try: step()
    except Exception as error:
        print(f"[{label}] Fehler: {type(error).__name__}: {error} - weiter mit den uebrigen Diensten.", flush=True)


def main() -> None:
    global COMFYUI_WATCHDOG
    if hasattr(sys.stdout, "reconfigure"): sys.stdout.reconfigure(line_buffering=True, errors="replace")
    parser = argparse.ArgumentParser(description="Start SeriesAI (local dashboard, LLM, ComfyUI, render worker) and keep the live monitor visible.")
    # The shared All-for-One launcher invokes every project as
    # ``start.py serve --yes``.  Accept that established contract while
    # retaining the direct ``python start.py`` invocation documented here.
    parser.add_argument("command", nargs="?", choices=["serve"], help=argparse.SUPPRESS)
    parser.add_argument("--yes", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--host", default="127.0.0.1", help="Nur lokal erreichbar (Standard); ein anderer Wert macht das Dashboard im Netzwerk sichtbar.")
    parser.add_argument("--port", default=PORT, type=int)
    parser.add_argument("--refresh-seconds", type=float, default=5.0)
    parser.add_argument("--no-install", action="store_true", help="Skip the Python package and model download/verification step.")
    parser.add_argument("--no-llm", action="store_true", help="Do not start llama.cpp (useful for dashboard-only diagnostics).")
    parser.add_argument("--no-render-worker", action="store_true", help="Do not start the optional configured local render worker.")
    args = parser.parse_args()
    if not acquire_instance_lock(): return
    # os.execv() keeps the PID (and therefore the lock) during a controlled
    # restart; normal exits and Ctrl+C release it for the next start.
    atexit.register(release_instance_lock)
    print("\n=== SeriesAI: lokaler Master & Live-Monitor ===\nProjekt: " + str(ROOT), flush=True)
    bootstrap()
    safe_step("Dashboard", ensure_dashboard_packages)
    local_url = f"http://127.0.0.1:{args.port}"; status = request_json(f"{local_url}/status")
    monitor_url = local_url; client_id = "master"; mode = "SeriesAI"

    def start_server() -> None:
        """The local dashboard/queue server runs as a child of this terminal (output prefixed [Server]) with the
        project venv. The All-for-One hub is the coordinator for everything that spans PCs, so there is no
        LAN discovery, joining or role election here any more."""
        nonlocal status
        status = request_json(f"{local_url}/status")
        if status:
            print(f"SeriesAI laeuft bereits (PID {status['pid']}).", flush=True); return
        SUPERVISOR.spawn("Server", [str(venv_python()), "-m", "seriesai.coordinator", "--host", args.host, "--port", str(args.port)], ROOT)
        for _ in range(240):   # a cold start from a network drive imports slowly
            time.sleep(.5); status = request_json(f"{local_url}/status")
            if status: break
        if not status: raise SystemExit("Der lokale Server konnte nicht gestartet werden; siehe logs/errors.log")
        print(f"SeriesAI gestartet (PID {status['pid']}) - Dashboard: {local_url}/", flush=True)

    # The hub checks that the dashboard answers, so bring it up first (minutes before the LLM has loaded) whenever
    # the venv already has what it needs; on a fresh machine install the packages first.
    server_early = _modules_available(["pydantic", "yaml"])
    if server_early: safe_step("Server", start_server)
    if not args.no_install:
        safe_step("Pip", ensure_python_packages); safe_step("Modelle", ensure_model_files)
    if not server_early: start_server()
    if not args.no_llm:
        safe_step("LLM", ensure_llm_server)
        safe_step("Vision", ensure_vision_server)
    if not args.no_render_worker:
        if not args.no_install: safe_step("ComfyUI", ensure_comfyui_install)
        safe_step("ComfyUI", ensure_comfyui)
        COMFYUI_WATCHDOG = True
        ensure_local_render_worker(monitor_url, client_id)
    monitor(monitor_url, max(1.0, args.refresh_seconds), client_id, mode, start_server)


if __name__ == "__main__": main()
