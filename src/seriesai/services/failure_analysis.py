"""Collect and diagnose only the failed work (analyses, jobs, render shots, render attempts) for the dashboard."""
from __future__ import annotations

import re
from pathlib import Path

from ..db import Database

# (pattern, cause, suggested action) - first match wins; purely local, rule-based.
RULES: list[tuple[str, str, str]] = [
    (r"abgebrochen|cancel", "Wurde absichtlich abgebrochen (z. B. neue Pipeline gestartet oder Neustart).",
     "Nichts zu reparieren; den Job bei Bedarf neu starten."),
    (r"database is locked", "SQLite war durch einen anderen Schreiber gesperrt (Netzlaufwerk/mehrere Prozesse).",
     "Meist voruebergehend: Auftrag erneut starten. Nur einen Koordinator gleichzeitig laufen lassen."),
    (r"did not complete shot within|ComfyUI.*(nicht|not) (mehr )?(erreichbar|kennt)|kennt den Auftrag|prompt_id", "ComfyUI hat den Shot nicht abgeschlossen (Timeout, Neustart oder verlorener Auftrag).",
     "recover-queue ausfuehren; ComfyUI-Start pruefen (ComfyUI-Fenster/Port 8188)."),
    (r"ComfyUI API (unavailable|request failed)|Connection refused|WinError 10061|nicht erreichbar", "ComfyUI bzw. der Koordinator war nicht erreichbar.",
     "Dienst starten (start.py) und danach den Shot erneut einreihen."),
    (r"out of memory|CUDA|OOM|allocate", "Zu wenig (GPU-)Speicher.",
     "work_intensity auf eco stellen oder hardware.prefer_cpu_offload/--lowvram beibehalten; Shot wiederholen."),
    (r"Permission|Zugriff verweigert|WinError 5", "Zugriff verweigert (Datei durch Virenscanner/Prozess gesperrt oder fehlende Rechte).",
     "Programme schliessen, die die Datei halten, dann erneut versuchen."),
    (r"No such file|FileNotFound|nicht gefunden|WinError 2", "Eine benoetigte Datei oder ein Modell fehlt.",
     "start.py ausfuehren (installiert fehlende Modelle/Abhaengigkeiten) und wiederholen."),
    (r"ffmpeg", "FFmpeg-Fehler beim Verarbeiten der Videodateien.", "ffmpeg im PATH pruefen; Quelldatei auf Beschaedigung testen."),
    (r"whisper|transcri", "Transkription fehlgeschlagen.", "Modell/Audio pruefen und transcribe fuer die Episode wiederholen."),
    (r"Ollama|llama|LLM|timed out|timeout", "Lokales Sprachmodell hat nicht rechtzeitig geantwortet.",
     "LLM-Server (llama.cpp) laeuft? Dann Schritt wiederholen; ggf. llm.timeout_seconds erhoehen."),
    (r"json|JSONDecode|Expecting", "Das Modell lieferte kein gueltiges JSON.", "Schritt wiederholen; bei Dauerfehler Prompt/Modell pruefen."),
]


def diagnose(message: str | None) -> dict:
    text = message or ""
    for pattern, cause, action in RULES:
        if re.search(pattern, text, re.IGNORECASE):
            return {"cause": cause, "action": action}
    return {"cause": "Ursache nicht erkannt." if text else "Kein Fehlertext gespeichert.", "action": "Fehlertext und logs/errors.log pruefen."}


def _log_lines(project_root: Path, needle: str, limit: int = 4) -> list[str]:
    """Last few errors.log lines that mention the given id (job/episode), to give context without opening the log."""
    path = project_root / "logs" / "errors.log"
    if not needle or not path.exists(): return []
    try: lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError: return []
    return [line[:300] for line in lines if needle in line][-limit:]


def collect_failures(database: Database, project_root: Path, limit: int = 100) -> dict:
    """Only failed rows - never completed, running, pending or cancelled ones."""
    with database.connect() as connection:
        analyses = [dict(r) for r in connection.execute(
            "SELECT analysis_run_id AS id, episode_id, stage_name AS name, error_message, finished_at AS at FROM analysis_runs "
            "WHERE status='failed' ORDER BY COALESCE(finished_at, started_at) DESC LIMIT ?", (limit,))]
        jobs = [dict(r) for r in connection.execute(
            "SELECT job_id AS id, series_id, job_type AS name, attempts, error_message, updated_at AS at FROM generation_jobs "
            "WHERE status='failed' ORDER BY updated_at DESC LIMIT ?", (limit,))]
        shots = [dict(r) for r in connection.execute(
            "SELECT item_id AS id, job_id, item_type, sequence_number, attempts, worker_id, error_message, COALESCE(finished_at, started_at) AS at "
            "FROM generation_job_items WHERE status='failed' ORDER BY COALESCE(finished_at, started_at) DESC LIMIT ?", (limit,))]
        attempts = [dict(r) for r in connection.execute(
            "SELECT attempt_id AS id, render_job_id AS job_id, attempt_number, profile, error_message, finished_at AS at FROM render_attempts "
            "WHERE status='failed' ORDER BY COALESCE(finished_at, started_at) DESC LIMIT ?", (limit,))]
    groups = {"analysen": analyses, "jobs": jobs, "shots": shots, "render_versuche": attempts}
    for rows in groups.values():
        for row in rows:
            row.update(diagnose(row.get("error_message")))
            row["log"] = _log_lines(project_root, str(row.get("job_id") or row["id"]))
    return {"counts": {name: len(rows) for name, rows in groups.items()}, "total": sum(len(rows) for rows in groups.values()), **groups}
