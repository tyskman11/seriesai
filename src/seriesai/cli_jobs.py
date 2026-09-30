"""Generic runner that exposes every seriesai.cli subcommand to the WebUI.

Introspects the same argparse parser the terminal uses, so a new CLI command
becomes usable from the dashboard automatically without extra WebUI code.
Each invocation runs as a detached local subprocess (the project's .venv
python), with its output and a small status sidecar written to
runtime/cli_jobs/<job_id>.* so the job list survives a coordinator restart.
"""
from __future__ import annotations

import json
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .cli import build_parser

JOBS_DIR = Path.cwd() / "runtime" / "cli_jobs"
POSITIONAL, STORE_TRUE, STORE_VALUE = "positional", "store_true", "store_value"


def _venv_python() -> Path:
    root = Path.cwd()
    candidate = root / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    return candidate if candidate.exists() else Path(sys.executable)


def describe_commands() -> list[dict]:
    parser = build_parser()
    subparsers_action = next(action for action in parser._actions if action.dest == "command")
    helps = {choice.dest: choice.help for choice in subparsers_action._choices_actions}
    commands = []
    for name, subparser in subparsers_action.choices.items():
        args = []
        for action in subparser._actions:
            if action.dest in ("help",):
                continue
            kind = STORE_TRUE if type(action).__name__ == "_StoreTrueAction" else (POSITIONAL if not action.option_strings else STORE_VALUE)
            default = action.default
            args.append({
                "dest": action.dest, "flag": action.option_strings[0] if action.option_strings else None,
                "kind": kind, "required": bool(action.required), "help": action.help or "",
                "default": None if default is None or isinstance(default, Path) else default,
                "type": "path" if action.type is Path else ("number" if action.type in (int, float) else "text"),
            })
        commands.append({"name": name, "help": helps.get(name, ""), "args": args})
    return sorted(commands, key=lambda item: item["name"])


def start(command: str, values: dict[str, str]) -> dict:
    known = {item["name"]: item for item in describe_commands()}
    if command not in known:
        raise ValueError(f"Unknown command: {command}")
    spec = known[command]
    argv = [command]
    for arg in spec["args"]:
        value = values.get(arg["dest"])
        if arg["kind"] == STORE_TRUE:
            if value: argv.append(arg["flag"])
            continue
        if value in (None, ""):
            if arg["required"]: raise ValueError(f"Missing required argument: {arg['flag'] or arg['dest']}")
            continue
        if arg["kind"] == POSITIONAL:
            argv.append(str(value))
        else:
            argv += [arg["flag"], str(value)]

    job_id = str(uuid.uuid4())
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = JOBS_DIR / f"{job_id}.log"
    meta_path = JOBS_DIR / f"{job_id}.json"
    meta = {"job_id": job_id, "command": command, "argv": argv, "status": "running", "started_at": datetime.now(timezone.utc).isoformat(), "finished_at": None, "returncode": None}
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    with log_path.open("wb") as handle:
        process = subprocess.Popen([str(_venv_python()), "-m", "seriesai.cli", *argv], cwd=Path.cwd(), stdout=handle, stderr=subprocess.STDOUT)
    meta["pid"] = process.pid
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    def watch() -> None:
        returncode = process.wait()
        meta["status"] = "completed" if returncode == 0 else "failed"
        meta["returncode"] = returncode
        meta["finished_at"] = datetime.now(timezone.utc).isoformat()
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    import threading
    threading.Thread(target=watch, daemon=True).start()
    return {"job_id": job_id, "argv": argv}


def status(job_id: str) -> dict:
    meta_path, log_path = JOBS_DIR / f"{job_id}.json", JOBS_DIR / f"{job_id}.log"
    if not meta_path.is_file():
        raise ValueError(f"Unknown job_id: {job_id}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    log = log_path.read_bytes().decode("utf-8", "replace") if log_path.is_file() else ""
    meta["log_tail"] = log[-8000:]
    return meta


def recent(limit: int = 30) -> list[dict]:
    if not JOBS_DIR.is_dir(): return []
    files = sorted(JOBS_DIR.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)[:limit]
    results = []
    for path in files:
        try: results.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError): continue
    return results
