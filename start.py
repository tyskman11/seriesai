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
import shutil
import threading
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
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

    def spawn(self, name: str, command: list[str], cwd: Path) -> subprocess.Popen:
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
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
COMFYUI_ARGS = ["--windows-standalone-build", "--enable-manager", "--lowvram", "--reserve-vram", "1"]


def configured_comfyui_root() -> Path | None:
    """The portable ComfyUI folder (parent of ``ComfyUI``) from ``paths.comfyui``."""
    match = re.search(r"(?ms)^paths:\s*(.*?)(?=^[A-Za-z_]+:|\Z)", (ROOT / "config.yaml").read_text(encoding="utf-8"))
    value = re.search(r"^\s*comfyui:\s*[\"']?([^\"'#\r\n]+?)[\"']?\s*(?:#.*)?$", match.group(1), re.MULTILINE) if match else None
    return (ROOT / value.group(1)).resolve().parent if value else None


def ensure_comfyui() -> None:
    """Start the local ComfyUI server inside this terminal unless it already answers."""
    if configured_video_backend() != "comfyui": return
    if request_json(f"{COMFYUI_URL}/system_stats"):
        print("[ComfyUI] laeuft bereits.", flush=True); return
    root = configured_comfyui_root()
    python = root / "python_embeded" / "python.exe" if root else None
    if not python or not python.exists():
        print("[ComfyUI] python_embeded nicht gefunden; bitte ComfyUI manuell starten.", flush=True); return
    SUPERVISOR.spawn("ComfyUI", [str(python), "-s", "ComfyUI/main.py", *COMFYUI_ARGS], root)


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
    worker = SUPERVISOR.spawn("Worker", [str(venv_python()), "-m", "seriesai.worker", "--master", master_url, "--worker-id", client_id], ROOT)
    state.write_text(json.dumps({"pid": worker.pid, "worker_id": client_id, "master_url": master_url}, indent=2), encoding="utf-8")


def ensure_ollama_model() -> None:
    """Check/download only the configured local Ollama model; never install Ollama."""
    model = configured_ollama_model()
    if not model:
        print("[Modelle] Kein Ollama-Modell konfiguriert; semantische Analyse bleibt deaktiviert.", flush=True)
        return
    base_url = "http://127.0.0.1:11434"
    tags = request_json(f"{base_url}/api/tags")
    if tags is None:
        executable = find_or_install_ollama()
        if not executable:
            print("[Modelle] Ollama ist nicht verfuegbar; Modell-Check uebersprungen.", flush=True)
            return
        print("[Modelle] Starte lokalen Ollama-Dienst ...", flush=True)
        SUPERVISOR.spawn("Ollama", [str(executable), "serve"], ROOT)
        for _ in range(20):
            time.sleep(.5); tags = request_json(f"{base_url}/api/tags")
            if tags is not None: break
        if tags is None:
            print("[Modelle] Ollama-Dienst konnte nicht gestartet werden.", flush=True)
            return
    available = {item.get("name") for item in tags.get("models", [])}
    if model in available:
        print(f"[Modelle] Ollama-Modell bereit: {model}", flush=True)
        return
    print(f"[Modelle] Lade fehlendes lokales Modell: {model}", flush=True)
    request = Request(f"{base_url}/api/pull", data=json.dumps({"name": model, "stream": True}).encode(), headers={"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=3600) as response:
            for raw in response:
                update = json.loads(raw)
                total, completed = update.get("total"), update.get("completed")
                if total and completed is not None:
                    print(f"[Modelle] {model}: {completed * 100 // total}%", flush=True)
                elif update.get("status"):
                    print(f"[Modelle] {update['status']}", flush=True)
    except OSError as error:
        print(f"[Modelle] Download fehlgeschlagen: {error}", flush=True)


def find_or_install_ollama() -> Path | None:
    found = shutil.which("ollama")
    if found:
        return Path(found)
    if os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
        if local.exists():
            return local
        print("[Vorbereitung] Ollama fehlt. Installiere es mit dem offiziellen Windows-Installer ...", flush=True)
        command = "irm https://ollama.com/install.ps1 | iex"
        try:
            subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command], check=True)
        except (OSError, subprocess.CalledProcessError) as error:
            print(f"[Vorbereitung] Ollama-Installation fehlgeschlagen: {error}", flush=True)
            return None
        return local if local.exists() else (Path(shutil.which("ollama")) if shutil.which("ollama") else None)
    print("[Vorbereitung] Ollama fehlt. Unter Linux wird wegen der Vorgabe 'kein sudo' keine systemweite Installation ausgefuehrt.", flush=True)
    print("[Vorbereitung] Lege eine benutzerlokale Ollama-Installation an und starte danach erneut.", flush=True)
    return None


def discover_coordinator(port: int) -> str | None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1); sock.settimeout(1.2)
        try:
            sock.sendto(b"SERIESAI_DISCOVER_V1", ("255.255.255.255", DISCOVERY_PORT))
            while True:
                response = json.loads(sock.recvfrom(2048)[0])
                if response.get("service") == "seriesai" and response.get("port") == port: return response["url"]
        except (OSError, TimeoutError, json.JSONDecodeError):
            return None


def human_duration(seconds: int | None) -> str:
    if seconds is None: return "wird berechnet"
    hours, remainder = divmod(max(0, seconds), 3600)
    return f"{hours} h {remainder // 60:02d} min"


def monitor(url: str, interval: float, client_id: str, mode: str) -> None:
    print(f"\nDashboard: {url}/")
    print(f"Dieser Rechner: {mode}")
    print("Live-Monitor aktiv. Alle Dienste laufen in diesem Terminal. Strg+C beendet sie alle.\n")
    restart_generation: int | None = None
    try:
        while True:
            SUPERVISOR.check(url)
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
                print(f"[{stamp}] Eigene Rolle: {own_role} | Verbundene Clients: {status.get('workers', 0)} | Transkripte {pipeline.get('transcripts', 0)}/{pipeline.get('audio', 0)} | Szenen {pipeline.get('scenes', 0)}/{pipeline.get('episodes', 0)} | {render} | Render-ETA {human_duration(pipeline.get('render_eta_seconds'))}\n           Jetzt: {current}", flush=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nBeende alle SeriesAI-Dienste ...", flush=True)
        SUPERVISOR.stop_all()
        print("Alles beendet. Unterbrochene Renderjobs setzt `recover-queue` fort.")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"): sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(description="Start or join SeriesAI and keep the live monitor visible.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", default=PORT, type=int)
    parser.add_argument("--join", metavar="URL")
    parser.add_argument("--refresh-seconds", type=float, default=5.0)
    parser.add_argument("--no-render-worker", action="store_true", help="Do not start the optional configured local render worker.")
    args = parser.parse_args()
    print("\n=== SeriesAI: lokaler Master & Live-Monitor ===\nProjekt: " + str(ROOT), flush=True)
    bootstrap()
    ensure_ollama_model()
    local_url = f"http://127.0.0.1:{args.port}"; status = request_json(f"{local_url}/status")
    monitor_url = local_url; client_id = "master"; mode = "Master & Koordinator"
    if not status:
        join_url = args.join or discover_coordinator(args.port)
        if join_url:
            name = os.environ.get("COMPUTERNAME") or socket.gethostname()
            client_id = f"{name.lower()}::{sys.platform.lower()}"
            result = request_json(f"{join_url.rstrip('/')}/workers/register", {"name": name, "platform": sys.platform, "client_id": client_id})
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
        ensure_comfyui()
        ensure_local_render_worker(monitor_url, client_id)
    monitor(monitor_url, max(1.0, args.refresh_seconds), client_id, mode)


if __name__ == "__main__": main()
