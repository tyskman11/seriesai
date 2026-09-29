from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
import threading
import socket
import sqlite3
import sys
import uuid
from urllib.parse import parse_qs, urlparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .db import Database
from .services.character_review import CharacterReviewService
from .services.relationship_review import RelationshipReviewService
from .services.profile_review import ProfileReviewService
from .config import load_config
from .services.render_readiness import render_readiness

LOCAL_ADDRESSES = {"127.0.0.1", "::1"}


class Coordinator:
    ROLES = {"coordinator-only", "coordinator-worker", "worker"}

    def __init__(self, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.host = host
        self.port = port
        self.state_path = Path.cwd() / "runtime" / "coordinator_state.json"
        self.profiles = self._load_profiles()
        self.restart_generation = self._load_restart_generation()
        master = self.profiles.get("master", {})
        self.workers: dict[str, dict] = {"master": {"name": os.environ.get("COMPUTERNAME") or "master", "platform": os.sys.platform, "role": master.get("role", "master-worker")}}
        self.lock = threading.Lock()
        self.admin_token = self._load_or_create_admin_token()
        self.background_job: dict = {"kind": None, "status": "idle", "detail": ""}

    def _load_or_create_admin_token(self) -> str:
        """One local-only admin token per project. Never sent over the network except as a header value."""
        token_path = Path.cwd() / "runtime" / "admin_token.txt"
        try:
            token = token_path.read_text(encoding="utf-8").strip()
            if token: return token
        except OSError:
            pass
        token = secrets.token_urlsafe(24)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(token, encoding="utf-8")
        return token

    def is_admin(self, presented_token: str | None) -> bool:
        return bool(presented_token) and hmac.compare_digest(presented_token, self.admin_token)

    def run_background(self, kind: str, target) -> bool:
        """Run one long admin action (embed/cluster/mux) at a time; status is polled from /status."""
        with self.lock:
            if self.background_job["status"] == "running": return False
            self.background_job = {"kind": kind, "status": "running", "detail": ""}
        def runner() -> None:
            try:
                detail = target()
                with self.lock: self.background_job = {"kind": kind, "status": "completed", "detail": detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)[:2000]}
            except Exception as error:
                with self.lock: self.background_job = {"kind": kind, "status": "failed", "detail": str(error)[:2000]}
        threading.Thread(target=runner, daemon=True).start()
        return True

    def _load_profiles(self) -> dict[str, dict]:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            # Migrate the early single-master setting without losing it.
            profiles = state.get("profiles", {})
            if not profiles and state.get("master_role"):
                profiles = {"master": {"role": state["master_role"]}}
            return {key: value for key, value in profiles.items() if value.get("role") in self.ROLES}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_profiles(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"profiles": self.profiles, "restart_generation": self.restart_generation}, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(self.state_path)

    def _load_restart_generation(self) -> int:
        try:
            return int(json.loads(self.state_path.read_text(encoding="utf-8")).get("restart_generation", 0))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return 0

    def status(self) -> dict:
        with self.lock:
            nodes = [{"id": worker_id, **worker} for worker_id, worker in self.workers.items()]
            result = {"pid": os.getpid(), "workers": len(self.workers), "worker_nodes": nodes, "master": self.workers["master"]["name"], "restart_generation": self.restart_generation, "background_job": dict(self.background_job)}
        result["pipeline"] = self._pipeline_status()
        return result

    @staticmethod
    def _pipeline_status() -> dict:
        database = Path.cwd() / "Series" / "series.db"
        if not database.exists(): return {"available": False}
        try:
            with sqlite3.connect(database, timeout=1) as connection:
                total = connection.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
                audio = connection.execute("SELECT COUNT(DISTINCT episode_id) FROM import_artifacts WHERE artifact_type='audio_16khz_mono' AND status='completed'").fetchone()[0]
                scenes = connection.execute("SELECT COUNT(DISTINCT episode_id) FROM analysis_runs WHERE stage_name='scene_detection' AND status='completed'").fetchone()[0]
                transcripts = connection.execute("SELECT COUNT(*) FROM transcripts WHERE status='completed'").fetchone()[0]
                active = [dict(episode_id=row[0], stage=row[1], started_at=row[2]) for row in connection.execute("SELECT episode_id, stage_name, started_at FROM analysis_runs WHERE status='running' ORDER BY started_at")]
                active += [dict(episode_id=row[0], stage=row[1], started_at=row[2], progress=row[3]) for row in connection.execute("SELECT job_id, job_type, updated_at, progress FROM generation_jobs WHERE status='running' ORDER BY updated_at")]
                semantic_completed = connection.execute("SELECT COUNT(*) FROM analysis_runs WHERE stage_name='semantic_analysis' AND status='completed'").fetchone()[0]
                semantic_failed = connection.execute("SELECT COUNT(*) FROM analysis_runs WHERE stage_name='semantic_analysis' AND status='failed'").fetchone()[0]
                semantic_failures = [dict(episode_id=row[0], error=row[1]) for row in connection.execute("SELECT episode_id, error_message FROM analysis_runs WHERE stage_name='semantic_analysis' AND status='failed' ORDER BY started_at")]
                candidate_count = connection.execute("SELECT COUNT(*) FROM character_candidates WHERE status='proposed'").fetchone()[0]
                approved_character_count = connection.execute("SELECT COUNT(*) FROM characters").fetchone()[0]
                profile_proposals = connection.execute("SELECT COUNT(*) FROM character_profile_proposals WHERE status='proposed'").fetchone()[0]
                relationship_proposals = connection.execute("SELECT COUNT(*) FROM relationship_proposals WHERE status='proposed'").fetchone()[0]
                try:
                    voice_clusters_total = connection.execute("SELECT COUNT(*) FROM voice_clusters").fetchone()[0]
                    voice_clusters_approved = connection.execute("SELECT COUNT(*) FROM voice_clusters WHERE status='approved'").fetchone()[0]
                except sqlite3.OperationalError:
                    voice_clusters_total = voice_clusters_approved = 0   # older DB not yet migrated
                queue_counts = {row[0]: row[1] for row in connection.execute("SELECT status,COUNT(*) FROM generation_job_items GROUP BY status")}
                rendering = connection.execute("SELECT job_id,sequence_number,worker_id,started_at,checkpoint_json FROM generation_job_items WHERE item_type='shot' AND status='rendering' ORDER BY started_at LIMIT 1").fetchone()
                completed_durations = connection.execute("SELECT AVG((julianday(finished_at)-julianday(started_at))*86400) FROM generation_job_items WHERE item_type='shot' AND status='completed' AND finished_at IS NOT NULL AND started_at IS NOT NULL").fetchone()[0]
                avg = connection.execute("SELECT AVG((julianday(finished_at)-julianday(started_at))*86400) FROM analysis_runs WHERE stage_name='transcription' AND status='completed' AND finished_at IS NOT NULL").fetchone()[0]
            readiness = render_readiness(load_config(Path.cwd() / "config.yaml"))
            total_shots = sum(queue_counts.values()); remaining = total_shots - queue_counts.get("completed", 0)
            current_shot = None if not rendering else {"job_id": rendering[0], "sequence_number": rendering[1], "worker_id": rendering[2], "started_at": rendering[3], "specification": json.loads(rendering[4] or "{}")}
            return {"available": True, "episodes": total, "audio": audio, "scenes": scenes, "transcripts": transcripts, "active": active, "semantic_completed": semantic_completed, "semantic_failed": semantic_failed, "semantic_failures": semantic_failures, "character_candidates": candidate_count, "approved_characters": approved_character_count, "profile_proposals": profile_proposals, "relationship_proposals": relationship_proposals, "voice_clusters_total": voice_clusters_total, "voice_clusters_approved": voice_clusters_approved, "queue_counts": queue_counts, "current_shot": current_shot, "render_total_shots": total_shots, "render_eta_seconds": int(completed_durations * remaining) if completed_durations and remaining else None, "render_readiness": readiness, "transcription_eta_seconds": int(avg * (audio - transcripts)) if avg and audio > transcripts else None}
        except sqlite3.Error as error:
            return {"available": False, "error": str(error)}

    def register(self, payload: dict) -> dict:
        name = payload.get("name", "unknown")
        platform = payload.get("platform", "unknown")
        worker_id = payload.get("client_id") or f"{name.lower()}::{platform.lower()}"
        with self.lock:
            saved = self.profiles.get(worker_id, {})
            self.workers[worker_id] = {"name": name, "platform": platform, "role": saved.get("role", "worker")}
            self.profiles[worker_id] = {"name": name, "platform": platform, "role": self.workers[worker_id]["role"]}
            self._save_profiles()
        return {"worker_id": worker_id}

    def set_role(self, worker_id: str, role: str) -> dict:
        if role not in self.ROLES:
            raise ValueError(f"Unknown worker role: {role}")
        with self.lock:
            if worker_id not in self.workers:
                raise KeyError(worker_id)
            if worker_id == "master" and role == "worker":
                raise ValueError("The master must remain a coordinator.")
            self.workers[worker_id]["role"] = role
            self.profiles[worker_id] = {**self.profiles.get(worker_id, {}), **self.workers[worker_id]}
            self._save_profiles()
            return {"worker_id": worker_id, "role": role}

    def restart(self) -> None:
        """Reload the coordinator code after replying to the dashboard request."""
        threading.Timer(0.25, self._restart_process).start()

    def restart_all(self) -> None:
        with self.lock:
            self.restart_generation += 1
            self._save_profiles()
        self.restart()

    def _restart_process(self) -> None:
        os.execv(sys.executable, [sys.executable, "-m", "seriesai.coordinator", "--host", self.host, "--port", str(self.port)])


def local_ip() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect(("192.0.2.1", 1))
            return sock.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def start_discovery_responder(host: str, port: int) -> None:
    advertised_host = local_ip() if host in {"0.0.0.0", "::"} else host
    response = json.dumps({"service": "seriesai", "port": port, "url": f"http://{advertised_host}:{port}"}).encode()
    def run() -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", 8766))
            while True:
                message, address = sock.recvfrom(2048)
                if message == b"SERIESAI_DISCOVER_V1": sock.sendto(response, address)
    threading.Thread(target=run, daemon=True).start()


def handler_factory(coordinator: Coordinator):
    class Handler(BaseHTTPRequestHandler):
        def _json(self, code: int, body: dict) -> None:
            encoded = json.dumps(body).encode()
            self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(encoded))); self.end_headers(); self.wfile.write(encoded)
        def _admin(self) -> bool:
            return coordinator.is_admin(self.headers.get("X-SeriesAI-Admin"))
        def _require_admin(self) -> bool:
            if self._admin(): return True
            self._json(403, {"error": "Nur der Koordinator (Admin) darf das."}); return False
        def _file(self, path: Path, content_type: str) -> None:
            root = Path.cwd().resolve(); resolved = path.resolve()
            if root not in resolved.parents or not resolved.is_file():
                return self._json(404, {"error": "file not found"})
            data = resolved.read_bytes()
            self.send_response(200); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(data))); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(data)
        def do_GET(self) -> None:
            if self.path == "/status":
                return self._json(200, coordinator.status())
            if self.path == "/":
                return self._page()
            if self.path == "/admin/token":
                # Only a request originating on this machine (the coordinator itself) can read
                # the admin token; a browser on another PC always sees this as 403 and stays read-only.
                if self.client_address[0] not in LOCAL_ADDRESSES:
                    return self._json(403, {"error": "admin token is only available on the coordinator machine"})
                return self._json(200, {"token": coordinator.admin_token})
            if self.path.startswith("/voice-clusters"):
                series_id = parse_qs(urlparse(self.path).query).get("series_id", [""])[0]
                if not series_id:
                    return self._json(400, {"error": "series_id is required"})
                from .services.voice_identity import VoiceIdentityService
                database = Database(Path.cwd() / "Series" / "series.db")
                try:
                    return self._json(200, {"clusters": VoiceIdentityService(load_config(Path.cwd() / "config.yaml"), database).review_clusters(series_id)})
                except (ValueError, sqlite3.Error) as error:
                    return self._json(400, {"error": str(error)})
            if self.path.startswith("/voice-sample/"):
                return self._file(Path.cwd() / "Series" / "voice_review" / self.path.removeprefix("/voice-sample/"), "audio/wav")
            if self.path.startswith("/character-candidates"):
                series_id = parse_qs(urlparse(self.path).query).get("series_id", [""])[0]
                if not series_id:
                    return self._json(400, {"error": "series_id is required"})
                database = Database(Path.cwd() / "Series" / "series.db")
                status = parse_qs(urlparse(self.path).query).get("status", ["proposed"])[0]
                return self._json(200, {"candidates": CharacterReviewService(database).candidates(series_id, None if status == "all" else status)})
            if self.path.startswith("/character-profile-proposals"):
                series_id = parse_qs(urlparse(self.path).query).get("series_id", [""])[0]
                if not series_id:
                    return self._json(400, {"error": "series_id is required"})
                database = Path.cwd() / "Series" / "series.db"
                with sqlite3.connect(database) as connection:
                    rows = connection.execute(
                        "SELECT p.proposal_id,p.character_id,c.canonical_name,p.profile_json,p.evidence_json,p.confidence,p.status,p.created_at "
                        "FROM character_profile_proposals p JOIN characters c ON c.character_id=p.character_id "
                        "WHERE c.series_id=? ORDER BY c.canonical_name",
                        (series_id,),
                    ).fetchall()
                return self._json(200, {"proposals": [{"proposal_id": row[0], "character_id": row[1], "canonical_name": row[2], "profile": json.loads(row[3]), "evidence": json.loads(row[4]), "confidence": row[5], "status": row[6], "created_at": row[7]} for row in rows]})
            if self.path.startswith("/relationship-proposals"):
                series_id = parse_qs(urlparse(self.path).query).get("series_id", [""])[0]
                if not series_id:
                    return self._json(400, {"error": "series_id is required"})
                database = Path.cwd() / "Series" / "series.db"
                with sqlite3.connect(database) as connection:
                    rows = connection.execute(
                        "SELECT p.proposal_id,a.canonical_name,b.canonical_name,p.summary_de,p.evidence_json,p.co_mentioned_event_count,p.confidence,p.status "
                        "FROM relationship_proposals p JOIN characters a ON a.character_id=p.character_a_id "
                        "JOIN characters b ON b.character_id=p.character_b_id WHERE p.series_id=? "
                        "ORDER BY p.co_mentioned_event_count DESC,a.canonical_name,b.canonical_name",
                        (series_id,),
                    ).fetchall()
                return self._json(200, {"proposals": [{"proposal_id": row[0], "character_a": row[1], "character_b": row[2], "summary_de": row[3], "evidence": json.loads(row[4]), "event_count": row[5], "confidence": row[6], "status": row[7]} for row in rows]})
            if self.path.startswith("/episode-evidence"):
                episode_id = parse_qs(urlparse(self.path).query).get("episode_id", [""])[0]
                if not episode_id:
                    return self._json(400, {"error": "episode_id is required"})
                database = Path.cwd() / "Series" / "series.db"
                with sqlite3.connect(database) as connection:
                    episode = connection.execute("SELECT episode_id,season_number,episode_number,title FROM episodes WHERE episode_id=?", (episode_id,)).fetchone()
                    events = connection.execute("SELECT event_type,description FROM world_events WHERE source_episode_id=? ORDER BY occurred_at,world_event_id LIMIT 30", (episode_id,)).fetchall()
                if not episode:
                    return self._json(404, {"error": "episode not found"})
                return self._json(200, {"episode": {"episode_id": episode[0], "season_number": episode[1], "episode_number": episode[2], "title": episode[3]}, "events": [{"event_type": row[0], "description": row[1]} for row in events]})
            if self.path.startswith("/render-queue/worker/") and self.path.endswith("/inflight"):
                worker_id = self.path.removeprefix("/render-queue/worker/").removesuffix("/inflight").strip("/")
                database = Path.cwd() / "Series" / "series.db"
                with sqlite3.connect(database) as connection:
                    row = connection.execute("SELECT item_id,job_id,sequence_number,checkpoint_json FROM generation_job_items WHERE worker_id=? AND status='rendering' ORDER BY started_at LIMIT 1", (worker_id,)).fetchone()
                return self._json(200, {"item": None if not row else {"item_id": row[0], "job_id": row[1], "sequence_number": row[2], "specification": json.loads(row[3] or "{}")}})
            if self.path == "/render-queue":
                database = Path.cwd() / "Series" / "series.db"
                with sqlite3.connect(database) as connection:
                    rows = connection.execute("SELECT i.item_id,i.job_id,i.sequence_number,i.status,i.worker_id,i.attempts,i.error_message,i.checkpoint_json FROM generation_job_items i WHERE i.item_type='shot' ORDER BY CASE i.status WHEN 'rendering' THEN 0 WHEN 'retry' THEN 1 WHEN 'pending' THEN 2 ELSE 3 END,i.job_id,i.sequence_number LIMIT 200").fetchall()
                return self._json(200, {"items": [{"item_id": row[0], "job_id": row[1], "sequence_number": row[2], "status": row[3], "worker_id": row[4], "attempts": row[5], "error_message": row[6], "specification": json.loads(row[7] or "{}") } for row in rows]})
            if self.path.startswith("/reference-image"):
                candidate_id = parse_qs(urlparse(self.path).query).get("candidate_id", [""])[0]
                database = Path.cwd() / "Series" / "series.db"
                try:
                    with sqlite3.connect(database) as connection:
                        row = connection.execute("SELECT file_path FROM visual_references WHERE entity_type='character_candidate' AND entity_id=? AND reference_kind IN ('review_face_crop','review_face') ORDER BY CASE reference_kind WHEN 'review_face_crop' THEN 0 ELSE 1 END LIMIT 1", (candidate_id,)).fetchone()
                    root = Path.cwd().resolve()
                    image = (root / row[0]).resolve() if row else None
                    if not image or root not in image.parents or not image.is_file():
                        return self._json(404, {"error": "reference image not found"})
                    data = image.read_bytes()
                    self.send_response(200); self.send_header("Content-Type", "image/jpeg"); self.send_header("Content-Length", str(len(data))); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(data)
                    return
                except (OSError, sqlite3.Error):
                    return self._json(404, {"error": "reference image not found"})
            return self._json(404, {"error": "not found"})
        def do_POST(self) -> None:
            # Raw binary upload from a remote render worker. Handle it before
            # JSON parsing; files stream to a temporary sibling and become
            # visible only after the whole upload has arrived.
            if self.path.startswith("/render-queue/items/") and self.path.endswith("/upload"):
                item_id = self.path.removeprefix("/render-queue/items/").removesuffix("/upload").strip("/")
                worker_id = self.headers.get("X-SeriesAI-Worker", "").strip()
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not worker_id or length <= 0 or length > 8 * 1024 * 1024 * 1024:
                        raise ValueError("A worker ID and a file up to 8 GB are required.")
                    database = Database(Path.cwd() / "Series" / "series.db")
                    with database.connect() as connection:
                        row = connection.execute("SELECT checkpoint_json FROM generation_job_items WHERE item_id=? AND status='rendering' AND worker_id=?", (item_id, worker_id)).fetchone()
                    if not row: raise ValueError("Item is not claimed by this worker.")
                    specification = json.loads(row[0] or "{}")
                    root = Path.cwd().resolve(); output = (root / specification["output_path"]).resolve()
                    if root not in output.parents: raise ValueError("Unsafe output path.")
                    output.parent.mkdir(parents=True, exist_ok=True); temporary = output.with_suffix(output.suffix + ".uploading")
                    remaining = length
                    with temporary.open("wb") as handle:
                        while remaining:
                            block = self.rfile.read(min(1024 * 1024, remaining))
                            if not block: raise ValueError("Upload ended before Content-Length.")
                            handle.write(block); remaining -= len(block)
                    temporary.replace(output)
                    from .services.queue_worker import QueueWorkerService
                    result = QueueWorkerService(database).complete(item_id, worker_id, str(specification["output_path"]))
                    return self._json(200, result)
                except (KeyError, ValueError, OSError, sqlite3.Error) as error:
                    return self._json(400, {"error": str(error)})
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if self.path.startswith("/voice-clusters/"):
                if not self._require_admin(): return
                from .services.voice_identity import VoiceIdentityService
                database = Database(Path.cwd() / "Series" / "series.db")
                service = VoiceIdentityService(load_config(Path.cwd() / "config.yaml"), database)
                try:
                    if self.path.endswith("/assign"):
                        cluster_id = self.path.removeprefix("/voice-clusters/").removesuffix("/assign").strip("/")
                        return self._json(200, service.assign_cluster(cluster_id, str(payload["character_id"])))
                    if self.path.endswith("/reject"):
                        cluster_id = self.path.removeprefix("/voice-clusters/").removesuffix("/reject").strip("/")
                        return self._json(200, service.reject_cluster(cluster_id))
                    if self.path == "/voice-clusters/embed":
                        series_id = str(payload["series_id"])
                        started = coordinator.run_background("embed_voices", lambda: service.embed_series(series_id))
                        return self._json(202 if started else 409, {"started": started})
                    if self.path == "/voice-clusters/cluster":
                        series_id = str(payload["series_id"])
                        started = coordinator.run_background("cluster_voices", lambda: service.cluster_series(series_id, bool(payload.get("force"))))
                        return self._json(202 if started else 409, {"started": started})
                    if self.path == "/voice-clusters/build-references":
                        series_id = str(payload["series_id"])
                        started = coordinator.run_background("build_voice_references", lambda: service.build_references(series_id))
                        return self._json(202 if started else 409, {"started": started})
                except (KeyError, ValueError) as error:
                    return self._json(400, {"error": str(error)})
                return self._json(404, {"error": "not found"})
            if self.path == "/reference-image/crop":
                if not self._require_admin(): return
                try:
                    candidate_id = str(payload["candidate_id"])
                    x, y = float(payload["x"]), float(payload["y"])
                    if not (0 <= x <= 1 and 0 <= y <= 1):
                        raise ValueError("Coordinates must be normalized values between 0 and 1.")
                    database = Path.cwd() / "Series" / "series.db"
                    with sqlite3.connect(database) as connection:
                        row = connection.execute("SELECT file_path FROM visual_references WHERE entity_type='character_candidate' AND entity_id=? AND reference_kind='review_face'", (candidate_id,)).fetchone()
                    root = Path.cwd().resolve(); source = (root / row[0]).resolve() if row else None
                    if not source or root not in source.parents or not source.is_file():
                        raise ValueError("Original reference frame not found.")
                    import cv2
                    image = cv2.imread(str(source))
                    if image is None:
                        raise ValueError("Reference frame cannot be read.")
                    height, width = image.shape[:2]; crop_width, crop_height = max(96, int(width * .36)), max(96, int(height * .62))
                    left = min(max(0, int(x * width - crop_width / 2)), max(0, width - crop_width)); top = min(max(0, int(y * height - crop_height / 2)), max(0, height - crop_height))
                    target = root / "Series" / "reference_images" / "review" / "candidates" / f"{candidate_id}.jpg"; target.parent.mkdir(parents=True, exist_ok=True)
                    if not cv2.imwrite(str(target), image[top:top + crop_height, left:left + crop_width]):
                        raise ValueError("Cropped reference frame could not be saved.")
                    with sqlite3.connect(database) as connection:
                        updated = connection.execute(
                            "UPDATE visual_references SET file_path=?,quality_score=? WHERE entity_type='character_candidate' AND entity_id=? AND reference_kind='review_face_crop'",
                            (target.relative_to(root).as_posix(), 1.0, candidate_id),
                        ).rowcount
                        if not updated:
                            connection.execute("INSERT INTO visual_references(reference_id,entity_type,entity_id,reference_kind,file_path,quality_score) VALUES(?,?,?,?,?,?)", (str(uuid.uuid4()), "character_candidate", candidate_id, "review_face_crop", target.relative_to(root).as_posix(), 1.0))
                        connection.commit()
                    return self._json(200, {"candidate_id": candidate_id, "status": "cropped"})
                except (KeyError, ValueError, OSError, sqlite3.Error) as error:
                    return self._json(400, {"error": str(error)})
            if self.path == "/workers/register":
                return self._json(201, coordinator.register(payload))
            if self.path.startswith("/workers/") and self.path.endswith("/role"):
                if not self._require_admin(): return
                worker_id = self.path.removeprefix("/workers/").removesuffix("/role").strip("/")
                try:
                    return self._json(200, coordinator.set_role(worker_id, payload.get("role", "")))
                except KeyError:
                    return self._json(404, {"error": "worker not found"})
                except ValueError as error:
                    return self._json(400, {"error": str(error)})
            if self.path.startswith("/render-queue/") and self.path.endswith("/claim"):
                job_id = self.path.removeprefix("/render-queue/").removesuffix("/claim").strip("/")
                worker_id = str(payload.get("worker_id", "")).strip()
                if not worker_id:
                    return self._json(400, {"error": "worker_id is required"})
                try:
                    from .services.queue_worker import QueueWorkerService
                    database = Database(Path.cwd() / "Series" / "series.db")
                    claimed = QueueWorkerService(database).claim_next(job_id, worker_id)
                    return self._json(200, {"item": claimed})
                except ValueError as error:
                    return self._json(400, {"error": str(error)})
            if self.path == "/render-queue/claim-next":
                worker_id = str(payload.get("worker_id", "")).strip()
                if not worker_id:
                    return self._json(400, {"error": "worker_id is required"})
                from .services.queue_worker import QueueWorkerService
                database = Database(Path.cwd() / "Series" / "series.db")
                return self._json(200, {"item": QueueWorkerService(database).claim_next_any(worker_id)})
            if self.path.startswith("/render-queue/items/") and self.path.endswith("/submission"):
                item_id = self.path.removeprefix("/render-queue/items/").removesuffix("/submission").strip("/")
                try:
                    from .services.queue_worker import QueueWorkerService
                    database = Database(Path.cwd() / "Series" / "series.db")
                    QueueWorkerService(database).record_backend_submission(item_id, str(payload["worker_id"]), str(payload.get("backend", "comfyui")), str(payload["prompt_id"]))
                    return self._json(200, {"item_id": item_id, "status": "submission_recorded"})
                except (KeyError, ValueError) as error:
                    return self._json(400, {"error": str(error)})
            if self.path.startswith("/render-queue/items/") and self.path.endswith("/complete"):
                item_id = self.path.removeprefix("/render-queue/items/").removesuffix("/complete").strip("/")
                try:
                    from .services.queue_worker import QueueWorkerService
                    database = Database(Path.cwd() / "Series" / "series.db")
                    result = QueueWorkerService(database).complete(item_id, str(payload["worker_id"]), str(payload["output_path"]))
                    return self._json(200, result)
                except (KeyError, ValueError) as error:
                    return self._json(400, {"error": str(error)})
            if self.path.startswith("/render-queue/items/") and self.path.endswith("/fail"):
                item_id = self.path.removeprefix("/render-queue/items/").removesuffix("/fail").strip("/")
                try:
                    from .services.queue_worker import QueueWorkerService
                    database = Database(Path.cwd() / "Series" / "series.db")
                    result = QueueWorkerService(database).fail(item_id, str(payload["worker_id"]), str(payload.get("error", "Unspecified local rendering error")), bool(payload.get("retry", True)))
                    return self._json(200, result)
                except (KeyError, ValueError) as error:
                    return self._json(400, {"error": str(error)})
            if self.path == "/control/restart":
                if not self._require_admin(): return
                coordinator.restart()
                return self._json(202, {"message": "Coordinator restart scheduled"})
            if self.path == "/control/restart-all":
                if not self._require_admin(): return
                coordinator.restart_all()
                return self._json(202, {"message": "All connected clients restart scheduled"})
            if self.path.startswith("/character-candidates/") and self.path.endswith("/approve"):
                if not self._require_admin(): return
                candidate_id = self.path.removeprefix("/character-candidates/").removesuffix("/approve").strip("/")
                try:
                    database = Database(Path.cwd() / "Series" / "series.db")
                    result = CharacterReviewService(database).approve(payload["series_id"], candidate_id, payload["character_id"], payload.get("canonical_name"))
                    return self._json(200, result)
                except (KeyError, ValueError) as error:
                    return self._json(400, {"error": str(error)})
            if self.path.startswith("/character-candidates/") and self.path.endswith("/reject"):
                if not self._require_admin(): return
                candidate_id = self.path.removeprefix("/character-candidates/").removesuffix("/reject").strip("/")
                try:
                    database = Database(Path.cwd() / "Series" / "series.db")
                    return self._json(200, CharacterReviewService(database).reject(payload["series_id"], candidate_id))
                except (KeyError, ValueError) as error:
                    return self._json(400, {"error": str(error)})
            if self.path.startswith("/relationship-proposals/") and self.path.endswith("/approve"):
                if not self._require_admin(): return
                proposal_id = self.path.removeprefix("/relationship-proposals/").removesuffix("/approve").strip("/")
                try:
                    database = Database(Path.cwd() / "Series" / "series.db")
                    return self._json(200, RelationshipReviewService(database).approve(payload["series_id"], proposal_id, payload.get("reviewer_note")))
                except (KeyError, ValueError) as error:
                    return self._json(400, {"error": str(error)})
            if self.path.startswith("/character-profile-proposals/") and self.path.endswith("/approve"):
                if not self._require_admin(): return
                proposal_id = self.path.removeprefix("/character-profile-proposals/").removesuffix("/approve").strip("/")
                try:
                    database = Database(Path.cwd() / "Series" / "series.db")
                    return self._json(200, ProfileReviewService(database).approve(proposal_id))
                except (KeyError, ValueError) as error:
                    return self._json(400, {"error": str(error)})
            if self.path.startswith("/relationship-proposals/") and self.path.endswith("/reject"):
                if not self._require_admin(): return
                proposal_id = self.path.removeprefix("/relationship-proposals/").removesuffix("/reject").strip("/")
                try:
                    database = Database(Path.cwd() / "Series" / "series.db")
                    return self._json(200, RelationshipReviewService(database).reject(payload["series_id"], proposal_id))
                except (KeyError, ValueError) as error:
                    return self._json(400, {"error": str(error)})
            return self._json(404, {"error": "not found"})
        def log_message(self, *_: object) -> None: pass

        def _page(self) -> None:
            page = """<!doctype html><html lang="de"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>SeriesAI Dashboard</title><style>body{font-family:system-ui;background:#10151f;color:#edf2f7;margin:0;padding:2rem}main{max-width:1100px;margin:auto}.muted{color:#a7b5c9}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1rem;margin:1.2rem 0}.card,li{background:#182130;border:1px solid #2d3b52;border-radius:12px;padding:1rem}.value{font-size:1.7rem;color:#69d4ff;font-weight:700}ul{padding:0;display:grid;gap:.6rem;list-style:none}button,select{background:#10151f;color:#edf2f7;border:1px solid #4b6385;border-radius:6px;padding:.45rem}button{color:#9ee493;cursor:pointer}.offline{color:#ff9b9b}.online{color:#9ee493}body:not(.is-admin) .admin-only,body:not(.is-admin) select,body:not(.is-admin) button:not(.view-ok){display:none!important}.role-badge{float:right;font-size:.85rem;padding:.25rem .6rem;border-radius:999px;border:1px solid #4b6385}.role-badge.admin{color:#9ee493;border-color:#9ee493}.role-badge.worker{color:#a7b5c9}audio{width:220px;vertical-align:middle}details.section{margin-top:1.2rem;border:none;padding:0}details.section>summary{cursor:pointer;font-weight:600;font-size:1rem;padding:.15rem 0;list-style:revert;color:#edf2f7}details.section>summary::marker{color:#69d4ff}details.section>summary h2,details.section>summary h3{display:inline;margin:0;font-size:1rem}details.section[open]>summary{margin-bottom:.6rem}details.section:not([open])>*:not(summary){display:none}</style><main><h1>SeriesAI Dashboard</h1><p class="muted">Lokaler Master &amp; Worker-Status · Aktualisierung alle 3 Sekunden</p><p id="connection" class="muted">Verbinde ...</p><button id="restart">WebUI &amp; Koordinator neu starten</button><section class="grid"><div class="card"><small>Master-PID</small><div class="value" id="pid">–</div></div><div class="card"><small>Clients</small><div class="value" id="workers">–</div></div><div class="card"><small>Episoden</small><div class="value" id="episodes">–</div></div><div class="card"><small>Audio</small><div class="value" id="audio">–</div></div><div class="card"><small>Szenen</small><div class="value" id="scenes">–</div></div><div class="card"><small>Transkripte</small><div class="value" id="transcripts">–</div></div><div class="card"><small>ETA</small><div class="value" id="eta">–</div></div></section><h2>Aktuelle Aufgaben</h2><ul id="active"><li class="muted">Keine Daten geladen.</li></ul><h2>Client-Rollen</h2><p class="muted">Rollen sind lokal persistent. Beim erneuten Beitritt wird die gespeicherte Rolle geladen.</p><ul id="nodes"></ul></main><script>const $=id=>document.getElementById(id),roles=['coordinator-only','coordinator-worker','worker'];function dur(s){if(s==null)return '–';return Math.floor(s/3600)+'h '+Math.floor(s%3600/60)+'m'}function esc(v){return String(v).replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]))}async function role(id,value){let r=await fetch('/workers/'+encodeURIComponent(id)+'/role',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({role:value})});if(!r.ok)alert((await r.json()).error);refresh()}async function refresh(){try{let s=await fetch('/status',{cache:'no-store'}).then(r=>{if(!r.ok)throw Error();return r.json()}),p=s.pipeline||{};$('connection').className='online';$('connection').textContent='ONLINE · Master '+s.master+' · PID '+s.pid;$('pid').textContent=s.pid;$('workers').textContent=s.workers;$('episodes').textContent=p.episodes??'–';$('audio').textContent=p.audio??'–';$('scenes').textContent=p.scenes??'–';$('transcripts').textContent=p.transcripts??'–';$('eta').textContent=dur(p.transcription_eta_seconds);$('active').innerHTML=(p.active?.length?p.active.map(x=>'<li><b>'+esc(x.stage)+'</b> · '+esc(x.episode_id)+' · seit '+esc(x.started_at)+'</li>'):['<li class="muted">Keine aktive Aufgabe.</li>']).join('');$('nodes').innerHTML=s.worker_nodes.map(x=>{let options=roles.filter(r=>x.id!=='master'||r!=='worker').map(r=>'<option value="'+r+'" '+(r===x.role?'selected':'')+'>'+r+'</option>').join('');return '<li><b>'+esc(x.name)+'</b> · '+esc(x.platform)+' · <select onchange="role(\''+esc(x.id)+'\',this.value)">'+options+'</select></li>'}).join('')}catch(e){$('connection').className='offline';$('connection').textContent='OFFLINE · Koordinator ist gestoppt oder nicht erreichbar.'}}$('restart').onclick=async()=>{let b=$('restart');b.disabled=true;$('connection').textContent='Koordinator wird neu gestartet ...';try{await fetch('/control/restart',{method:'POST'});setTimeout(refresh,1300)}finally{setTimeout(()=>b.disabled=false,1400)}};refresh();setInterval(refresh,3000)</script></html>"""
            page += """<script>const byId=id=>document.getElementById(id);const safe=v=>String(v).replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]));async function refreshDashboard(){try{const s=await fetch('/status',{cache:'no-store'}).then(r=>{if(!r.ok)throw Error();return r.json()}),p=s.pipeline||{};byId('connection').className='online';byId('connection').textContent='ONLINE | Master '+s.master+' | PID '+s.pid;for(const [id,value] of Object.entries({pid:s.pid,workers:s.workers,episodes:p.episodes??'-',audio:p.audio??'-',scenes:p.scenes??'-',transcripts:p.transcripts??'-',eta:p.transcription_eta_seconds==null?'-':Math.floor(p.transcription_eta_seconds/3600)+'h '+Math.floor(p.transcription_eta_seconds%3600/60)+'m'}))byId(id).textContent=value;byId('active').innerHTML=(p.active?.length?p.active.map(x=>'<li><b>'+safe(x.stage)+'</b> | '+safe(x.episode_id)+'</li>'):['<li class="muted">Keine aktive Aufgabe.</li>']).join('');byId('nodes').innerHTML=s.worker_nodes.map(x=>'<li><b>'+safe(x.name)+'</b> | '+safe(x.platform)+' | <select data-worker="'+safe(x.id)+'">'+['coordinator-only','coordinator-worker','worker'].filter(r=>x.id!=='master'||r!=='worker').map(r=>'<option '+(r===x.role?'selected':'')+' value="'+r+'">'+r+'</option>').join('')+'</select></li>').join('');document.querySelectorAll('[data-worker]').forEach(select=>select.onchange=async()=>{await fetch('/workers/'+encodeURIComponent(select.dataset.worker)+'/role',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({role:select.value})});refreshDashboard()})}catch(e){byId('connection').className='offline';byId('connection').textContent='OFFLINE | Koordinator ist gestoppt oder nicht erreichbar.'}}byId('restart').onclick=async()=>{await fetch('/control/restart',{method:'POST'});setTimeout(refreshDashboard,1300)};refreshDashboard();setInterval(refreshDashboard,3000)</script>"""
            page += """<script>const restartButton=document.getElementById('restart');restartButton.insertAdjacentHTML('afterend',' <button id="restartAll">Alle verbundenen Clients neu starten</button>');document.getElementById('restartAll').onclick=async()=>{const b=document.getElementById('restartAll');b.disabled=true;await fetch('/control/restart-all',{method:'POST'});setTimeout(()=>b.disabled=false,2500)}</script>"""
            page += """<script>(function(){const individual=document.getElementById('restart');const all=document.getElementById('restartAll');if(individual)individual.remove();if(!all)return;all.textContent='Alles neu starten';all.title='Startet Koordinator, WebUI und alle verbundenen Clients im selben Durchlauf neu.';all.onclick=async()=>{all.disabled=true;try{await fetch('/control/restart-all',{method:'POST'});}finally{setTimeout(()=>all.disabled=false,3000)}}})();</script>"""
            page += """<script>(function(){const grid=document.querySelector('.grid');grid.insertAdjacentHTML('beforeend','<div class="card"><small>Profilvorschlaege</small><div class="value" id="profileProposals">–</div></div><div class="card"><small>Beziehungsbelege</small><div class="value" id="relationshipProposals">–</div></div>');async function proposals(){try{const p=(await fetch('/status',{cache:'no-store'}).then(r=>r.json())).pipeline||{};document.getElementById('profileProposals').textContent=p.profile_proposals??0;document.getElementById('relationshipProposals').textContent=p.relationship_proposals??0}catch(e){}}proposals();setInterval(proposals,3000)})();</script>"""
            page += """<script>(function(){const modal=document.createElement('dialog');modal.style.cssText='max-width:min(92vw,1200px);background:#182130;color:#edf2f7;border:1px solid #4b6385;border-radius:12px;padding:1rem';modal.innerHTML='<button class="view-ok" style="float:right" aria-label="Schliessen">Schliessen</button><h2 id="referenceTitle">Referenzbild</h2><p class="muted">Bei mehreren Personen: Klicke direkt auf die gemeinte Person. SeriesAI speichert daraus einen lokalen Referenzausschnitt fuer diesen Kandidaten.</p><img id="referenceLarge" style="display:block;max-width:88vw;max-height:72vh;object-fit:contain">';document.body.append(modal);modal.querySelector('button').onclick=()=>modal.close();document.addEventListener('click',event=>{const image=event.target.closest('img[src^="/reference-image"]');if(!image)return;document.getElementById('referenceTitle').textContent=image.alt||'Referenzbild';document.getElementById('referenceLarge').src=image.src;modal.showModal()})})();</script>"""
            page += """<script>(function(){const modal=document.querySelector('dialog'),image=document.getElementById('referenceLarge');document.addEventListener('click',event=>{const thumbnail=event.target.closest('img[src^="/reference-image"]');if(thumbnail)modal.dataset.candidateId=new URL(thumbnail.src).searchParams.get('candidate_id')||''},true);image.title='Klicke die gemeinte Person, um einen lokalen Referenzausschnitt zu speichern.';image.style.cursor='crosshair';image.addEventListener('click',async event=>{const candidateId=modal.dataset.candidateId;if(!candidateId)return;const rect=image.getBoundingClientRect(),x=(event.clientX-rect.left)/rect.width,y=(event.clientY-rect.top)/rect.height;const response=await fetch('/reference-image/crop',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({candidate_id:candidateId,x,y})});if(!response.ok){alert((await response.json()).error);return}document.querySelectorAll('img[src^="/reference-image"]').forEach(node=>{if(new URL(node.src).searchParams.get('candidate_id')===candidateId)node.src='/reference-image?candidate_id='+encodeURIComponent(candidateId)+'&v='+Date.now()});modal.close()})})();</script>"""
            page += """<script>(function(){const grid=document.querySelector('.grid'),nodes=document.getElementById('nodes');grid.insertAdjacentHTML('beforeend','<div class="card"><small>Series Brain fertig</small><div class="value" id="semanticDone">–</div></div><div class="card"><small>Pruefung offen</small><div class="value" id="semanticFailed">–</div></div>');nodes.insertAdjacentHTML('beforebegin','<h2>Series-Brain-Analyse</h2><p class="muted" id="semanticStatus">Lade Abschlussstatus …</p><ul id="semanticFailures"></ul>');async function semanticStatus(){try{const p=(await fetch('/status',{cache:'no-store'}).then(r=>r.json())).pipeline||{};document.getElementById('semanticDone').textContent=(p.semantic_completed??0)+' / '+(p.transcripts??0);document.getElementById('semanticFailed').textContent=p.semantic_failed??0;document.getElementById('semanticStatus').textContent=(p.semantic_completed===p.transcripts&&!(p.semantic_failed))?'Alle Transkripte wurden in Series-Brain-Ereignisse uebernommen.':((p.semantic_completed??0)+' abgeschlossen, '+(p.semantic_failed??0)+' zur erneuten Pruefung offen.');document.getElementById('semanticFailures').innerHTML=(p.semantic_failures||[]).map(x=>'<li><b>'+x.episode_id+'</b> · lokale JSON-Ausgabe unvollstaendig; wird beim naechsten Analyseaufruf erneut versucht.</li>').join('')}catch(e){}}semanticStatus();setInterval(semanticStatus,3000)})();</script>"""
            page += """<script>(function(){const nodes=document.getElementById('nodes');nodes.insertAdjacentHTML('beforebegin','<h2>Figuren-Pruefung</h2><p class="muted" id="characterReviewStatus">Lade Figuren-Vorschlaege …</p><ul id="characterCandidates"></ul>');const esc=v=>String(v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));const slug=v=>String(v).toLowerCase().normalize('NFD').replace(/[\\u0300-\\u036f]/g,'').replace(/[^a-z0-9]+/g,'_').replace(/^_|_$/g,'');async function loadCandidates(){try{const s=await fetch('/status',{cache:'no-store'}).then(r=>r.json()),p=s.pipeline||{},series='game_shakers',data=await fetch('/character-candidates?series_id='+series,{cache:'no-store'}).then(r=>r.json());document.getElementById('characterReviewStatus').textContent=(p.approved_characters||0)+' feste Figuren, '+(data.candidates||[]).length+' Vorschlaege. Das Bild stammt aus einer Beleg-Episode und muss visuell bestaetigt werden.';document.getElementById('characterCandidates').innerHTML=(data.candidates||[]).map(x=>'<li><img src="/reference-image?candidate_id='+encodeURIComponent(x.candidate_id)+'" alt="Referenzbild aus Beleg-Episode fuer '+esc(x.canonical_name)+'" style="width:128px;height:72px;object-fit:cover;float:right;border-radius:6px" onerror="this.hidden=true"> <b>'+esc(x.canonical_name)+'</b> · '+Math.round(x.confidence*100)+'% · '+esc((x.evidence||[])[0]||'')+'<br><small class="muted">Gesicht aus Beleg-Episode – noch nicht automatisch namenverifiziert.</small><br><input data-id="'+esc(x.candidate_id)+'" value="'+esc(slug(x.canonical_name))+'_001" aria-label="Interne ID '+esc(x.canonical_name)+'"> <button data-approve="'+esc(x.candidate_id)+'">Freigeben</button> <button data-reject="'+esc(x.candidate_id)+'">Ablehnen</button><div style="clear:both"></div></li>').join('')||'<li class="muted">Keine offenen Vorschlaege.</li>';document.querySelectorAll('[data-approve]').forEach(b=>b.onclick=async()=>{const id=b.dataset.approve,input=document.querySelector('[data-id="'+id+'"]'),r=await fetch('/character-candidates/'+encodeURIComponent(id)+'/approve',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({series_id:series,character_id:input.value})});if(!r.ok)alert((await r.json()).error);loadCandidates()});document.querySelectorAll('[data-reject]').forEach(b=>b.onclick=async()=>{const r=await fetch('/character-candidates/'+encodeURIComponent(b.dataset.reject)+'/reject',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({series_id:series})});if(!r.ok)alert((await r.json()).error);loadCandidates()})}catch(e){document.getElementById('characterReviewStatus').textContent='Figuren-Vorschlaege konnten nicht geladen werden.'}}loadCandidates();setInterval(loadCandidates,5000)})();</script>"""
            page += """<script>(function(){const open=document.getElementById('characterCandidates');open.insertAdjacentHTML('afterend','<h3>Freigegebene Figuren</h3><ul id="approvedCharacters"></ul>');async function loadApproved(){try{const data=await fetch('/character-candidates?series_id=game_shakers&status=all',{cache:'no-store'}).then(r=>r.json()),approved=(data.candidates||[]).filter(x=>x.status==='approved');document.getElementById('approvedCharacters').innerHTML=approved.map(x=>'<li><img src="/reference-image?candidate_id='+encodeURIComponent(x.candidate_id)+'" alt="Referenzbild fuer '+x.canonical_name+'" style="width:128px;height:72px;object-fit:cover;float:right;border-radius:6px" onerror="this.hidden=true"> <b>'+x.canonical_name+'</b> · freigegeben<br><small class="muted">Referenzbild aus Beleg-Episode; visuell zu kontrollieren.</small><div style="clear:both"></div></li>').join('')||'<li class="muted">Noch keine freigegebenen Figuren.</li>'}catch(e){}}loadApproved();setInterval(loadApproved,5000)})();</script>"""
            page += """<script>(function(){const anchor=document.getElementById('nodes');anchor.insertAdjacentHTML('beforebegin','<h2>Beziehungsbelege</h2><p class="muted">Gemeinsame Nennungen aus gespeicherten Ereignissen. Erst dein Klick auf Freigeben schreibt eine kanonische Beziehungshistorie.</p><ul id="relationshipProposalsList"></ul>');const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));const modal=document.createElement('dialog');modal.style.cssText='max-width:min(92vw,900px);background:#182130;color:#edf2f7;border:1px solid #4b6385;border-radius:12px;padding:1rem';modal.innerHTML='<button class="view-ok" style="float:right" aria-label="Belegdialog schliessen">Schliessen</button><h2 id="evidenceTitle">Episode</h2><ul id="episodeEvidenceList"></ul>';document.body.append(modal);modal.querySelector('button').onclick=()=>modal.close();async function showEpisode(id){const data=await fetch('/episode-evidence?episode_id='+encodeURIComponent(id),{cache:'no-store'}).then(r=>r.json());document.getElementById('evidenceTitle').textContent=data.episode.episode_id+(data.episode.title?' · '+data.episode.title:'');document.getElementById('episodeEvidenceList').innerHTML=(data.events||[]).map(e=>'<li><b>'+esc(e.event_type||'Ereignis')+'</b><br>'+esc(e.description)+'</li>').join('')||'<li class="muted">Keine gespeicherten Ereignisse.</li>';modal.showModal()}async function review(id,action){const r=await fetch('/relationship-proposals/'+encodeURIComponent(id)+'/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({series_id:'game_shakers'})});if(!r.ok)alert((await r.json()).error);loadRelationships()}async function loadRelationships(){try{const data=await fetch('/relationship-proposals?series_id=game_shakers',{cache:'no-store'}).then(r=>r.json());document.getElementById('relationshipProposalsList').innerHTML=(data.proposals||[]).map(x=>{const episodes=[...new Set((x.evidence||[]).map(e=>e.episode_id).filter(Boolean))],chips=episodes.map(id=>'<button class="episodeEvidence view-ok" data-episode="'+esc(id)+'">'+esc(id)+'</button>').join(' '),controls=x.status==='proposed'?'<br><button data-relationship-approve="'+esc(x.proposal_id)+'">Freigeben</button> <button data-relationship-reject="'+esc(x.proposal_id)+'">Ablehnen</button>':'<br><small class="muted">'+esc(x.status)+'</small>';return '<li><b>'+esc(x.character_a)+' ↔ '+esc(x.character_b)+'</b> · '+Number(x.event_count)+' Ereignis(se) · '+Math.round(Number(x.confidence)*100)+'%<p>'+esc(x.summary_de)+'</p><small><b>Beleg-Episoden:</b> '+(chips||'–')+'</small>'+controls+'</li>'}).join('')||'<li class="muted">Noch keine Beziehungsbelege.</li>';document.querySelectorAll('.episodeEvidence').forEach(button=>button.onclick=()=>showEpisode(button.dataset.episode));document.querySelectorAll('[data-relationship-approve]').forEach(button=>button.onclick=()=>review(button.dataset.relationshipApprove,'approve'));document.querySelectorAll('[data-relationship-reject]').forEach(button=>button.onclick=()=>review(button.dataset.relationshipReject,'reject'))}catch(e){document.getElementById('relationshipProposalsList').innerHTML='<li class="muted">Beziehungsbelege konnten nicht geladen werden.</li>'}}loadRelationships();setInterval(loadRelationships,5000)})();</script>"""
            page += """<script>(function(){const anchor=document.getElementById('nodes');anchor.insertAdjacentHTML('beforebegin','<h2>Profil-Vorschlaege</h2><p class="muted">Lokale Ollama-Auswertung aus Serienbelegen. Erst deine Freigabe schreibt nicht-leere Werte in den kanonischen Figurenstand.</p><ul id="profileProposalsList"></ul>');const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));const traits=o=>Object.entries(o||{}).map(([k,v])=>esc(k)+' '+Math.round(Number(v)*100)+'%').join(' · ')||'–';async function approveProfile(id){const r=await fetch('/character-profile-proposals/'+encodeURIComponent(id)+'/approve',{method:'POST'});if(!r.ok)alert((await r.json()).error);loadProfiles()}async function loadProfiles(){try{const data=await fetch('/character-profile-proposals?series_id=game_shakers',{cache:'no-store'}).then(r=>r.json());document.getElementById('profileProposalsList').innerHTML=(data.proposals||[]).map(x=>{const p=x.profile||{},control=x.status==='proposed'?'<br><button data-profile-approve="'+esc(x.proposal_id)+'">Profil freigeben</button>':' ';return '<li><b>'+esc(x.canonical_name)+'</b> · '+Math.round(Number(x.confidence)*100)+'% · <span class="muted">'+esc(x.status)+'</span><p>'+esc(p.summary_de)+'</p><small><b>Kern:</b> '+traits(p.core_traits)+'<br><b>Entwickelt:</b> '+traits(p.developed_traits)+'<br><b>Ziele:</b> '+esc((p.current_goals||[]).join(' · ')||'–')+'<br><b>Belege:</b> '+esc((x.evidence||p.evidence||[]).join(' · ')||'–')+'</small>'+control+'</li>'}).join('')||'<li class="muted">Noch keine Profil-Vorschlaege.</li>';document.querySelectorAll('[data-profile-approve]').forEach(b=>b.onclick=()=>approveProfile(b.dataset.profileApprove))}catch(e){document.getElementById('profileProposalsList').innerHTML='<li class="muted">Profil-Vorschlaege konnten nicht geladen werden.</li>'}}loadProfiles();setInterval(loadProfiles,5000)})();</script>"""
            page += """<script>(function(){const anchor=document.getElementById('nodes');anchor.insertAdjacentHTML('beforebegin','<h2>Render-Queue</h2><p class="muted" id="queueSummary">Lade lokale Shot-Queue …</p><ul id="renderQueue"></ul>');const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));const eta=s=>s==null?'wird nach dem ersten fertigen Shot berechnet':Math.floor(s/3600)+'h '+Math.floor(s%3600/60)+'m';async function queue(){try{const [status,data]=await Promise.all([fetch('/status',{cache:'no-store'}).then(r=>r.json()),fetch('/render-queue',{cache:'no-store'}).then(r=>r.json())]),p=status.pipeline||{},counts=p.queue_counts||{},shot=p.current_shot;document.getElementById('queueSummary').textContent='Fortschritt: '+(counts.completed||0)+' / '+(p.render_total_shots||0)+' · Pending: '+(counts.pending||0)+' · Rendering: '+(counts.rendering||0)+' · Retry: '+(counts.retry||0)+' · ETA: '+eta(p.render_eta_seconds)+(shot?' · Aktuell: Shot '+shot.sequence_number+' bei '+(shot.specification.location||'unbekannt')+' ('+(shot.worker_id||'–')+')':'');document.getElementById('renderQueue').innerHTML=(data.items||[]).map(x=>'<li><b>'+esc(x.job_id)+' #'+x.sequence_number+'</b> · '+esc(x.status)+' · Worker: '+esc(x.worker_id||'–')+' · Versuche: '+Number(x.attempts||0)+'<br><small>'+esc(x.specification.location||'–')+' · '+esc(x.specification.duration_seconds||'?')+'s · '+esc(x.error_message||'')+'</small></li>').join('')||'<li class="muted">Noch keine Shot-Queue angelegt.</li>'}catch(e){document.getElementById('queueSummary').textContent='Queue nicht erreichbar.'}}queue();setInterval(queue,3000)})();</script>"""
            page += """<script>(function(){
const nativeFetch=window.fetch.bind(window);let adminToken=null;
window.fetch=(url,options)=>{options=options||{};if(adminToken&&typeof url==='string'&&!url.startsWith('/admin/token')){options.headers={...(options.headers||{}),'X-SeriesAI-Admin':adminToken}}return nativeFetch(url,options)};
async function detectAdmin(){try{const r=await nativeFetch('/admin/token',{cache:'no-store'});if(!r.ok)throw Error();adminToken=(await r.json()).token;document.body.classList.add('is-admin')}catch(e){document.body.classList.remove('is-admin')}
const h1=document.querySelector('h1');if(h1&&!document.getElementById('roleBadge')){const badge=document.createElement('span');badge.id='roleBadge';badge.className='role-badge '+(adminToken?'admin':'worker');badge.textContent=adminToken?'ADMIN . Koordinator':'ANSICHT . Worker (nur lesen)';h1.appendChild(badge)}else if(document.getElementById('roleBadge')){document.getElementById('roleBadge').className='role-badge '+(adminToken?'admin':'worker');document.getElementById('roleBadge').textContent=adminToken?'ADMIN . Koordinator':'ANSICHT . Worker (nur lesen)'}}
detectAdmin();setInterval(detectAdmin,10000);
})();</script>"""
            page += """<script>(function(){const anchor=document.getElementById('nodes');anchor.insertAdjacentHTML('beforebegin','<h2>Stimmen-Pruefung</h2><p class="muted" id="voiceStatus">Lade Stimmen-Cluster ...</p><p class="admin-only"><button data-voice-embed>1. Sprecher-Embeddings berechnen</button> <button data-voice-cluster>2. Zu Stimmen gruppieren</button> <button data-voice-refs>4. Referenzclips fuer TTS bauen</button></p><ul id="voiceClusters"></ul>');
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const seriesId='game_shakers';let characters=[];
async function loadCharacters(){try{const data=await fetch('/character-candidates?series_id='+seriesId+'&status=all',{cache:'no-store'}).then(r=>r.json());characters=(data.candidates||[]).filter(x=>x.status==='approved')}catch(e){}}
async function trigger(kind,path){try{const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({series_id:seriesId})});if(r.status===409){alert('Es laeuft bereits eine Hintergrundaufgabe.');return}if(!r.ok){alert((await r.json()).error||'Fehlgeschlagen');return}alert(kind+' laeuft jetzt im Hintergrund; Fortschritt siehe Statuszeile oben.')}catch(e){alert('Anfrage fehlgeschlagen.')}}
async function assign(clusterId,characterId){if(!characterId)return;const r=await fetch('/voice-clusters/'+encodeURIComponent(clusterId)+'/assign',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({character_id:characterId})});if(!r.ok)alert((await r.json()).error);loadVoices()}
async function reject(clusterId){const r=await fetch('/voice-clusters/'+encodeURIComponent(clusterId)+'/reject',{method:'POST',headers:{'Content-Type':'application/json'}});if(!r.ok)alert((await r.json()).error);loadVoices()}
async function loadVoices(){try{await loadCharacters();const s=await fetch('/status',{cache:'no-store'}).then(r=>r.json()),p=s.pipeline||{},job=s.background_job||{};document.getElementById('voiceStatus').textContent=(p.voice_clusters_approved||0)+' von '+(p.voice_clusters_total||0)+' Stimmen zugeordnet.'+(job.status==='running'?' Hintergrundaufgabe laeuft: '+job.kind+' ...':'');
const data=await fetch('/voice-clusters?series_id='+seriesId,{cache:'no-store'}).then(r=>r.ok?r.json():{clusters:[]});
const options='<option value="">Figur waehlen ...</option>'+characters.map(c=>'<option value="'+esc(c.character_id)+'">'+esc(c.canonical_name)+'</option>').join('');
document.getElementById('voiceClusters').innerHTML=(data.clusters||[]).sort((a,b)=>b.total_seconds-a.total_seconds).map(c=>{
const players=(c.sample_files||[]).map((f,i)=>'<audio controls preload="none" src="/voice-sample/'+f.split('voice_review/')[1]+'"></audio> <small>'+esc((c.sample_texts||[])[i]||'')+'</small><br>').join('');
const controls=c.status==='approved'?('<b>Zugeordnet:</b> '+esc(c.character_id)+' <button class="admin-only" data-voice-reject="'+esc(c.cluster_id)+'">Ablehnen</button>'):('<select class="admin-only" data-voice-assign="'+esc(c.cluster_id)+'">'+options+'</select> <button class="admin-only" data-voice-reject="'+esc(c.cluster_id)+'">Ablehnen</button>'+(c.status==='rejected'?' <small class="muted">(abgelehnt – oben erneut zuordnen moeglich)</small>':''));
return '<li><b>'+esc(c.cluster_id)+'</b> . '+Math.round(c.total_seconds/60)+' min . '+c.segment_count+' Segmente<br>'+players+controls+'</li>'
}).join('')||'<li class="muted">Noch keine Stimmen-Cluster. Erst Embeddings berechnen, dann gruppieren.</li>';
document.querySelectorAll('[data-voice-assign]').forEach(select=>select.onchange=()=>assign(select.dataset.voiceAssign,select.value));
document.querySelectorAll('[data-voice-reject]').forEach(button=>button.onclick=()=>reject(button.dataset.voiceReject))
}catch(e){document.getElementById('voiceStatus').textContent='Stimmen-Cluster konnten nicht geladen werden.'}}
document.addEventListener('click',e=>{if(e.target.matches('[data-voice-embed]'))trigger('Embeddings','/voice-clusters/embed');if(e.target.matches('[data-voice-cluster]'))trigger('Gruppierung','/voice-clusters/cluster');if(e.target.matches('[data-voice-refs]'))trigger('Referenzclips','/voice-clusters/build-references')});
loadVoices();setInterval(loadVoices,6000)})();</script>"""
            page += """<script>(function(){const anchor=document.getElementById('nodes');anchor.insertAdjacentHTML('beforebegin','<h2>Lokale Render-Bereitschaft</h2><p class="muted" id="renderReadiness">Pruefe ComfyUI und lokale Wan-Dateien …</p>');async function checkRender(){try{const p=(await fetch('/status',{cache:'no-store'}).then(r=>r.json())).pipeline||{},r=p.render_readiness||{};const models=[...(r.wan_models||[]),...(r.text_encoders||[]),...(r.vaes||[])];document.getElementById('renderReadiness').textContent=r.ready?'Bereit: lokaler ComfyUI-Renderer kann Queue-Shots ausfuehren.':('Status: '+(r.state||'unbekannt')+' · ComfyUI API: '+(r.api_online?'online':'offline')+' · Workflow: '+(r.workflow_configured?'konfiguriert':'nicht konfiguriert')+' · lokale Dateien: '+(models.join(', ')||'keine')+' · Naechster Schritt: '+(r.required_next_step||'–'));}catch(e){document.getElementById('renderReadiness').textContent='Render-Bereitschaft konnte nicht gelesen werden.'}}checkRender();setInterval(checkRender,5000)})();</script>"""
            page += """<script>(function(){
function wrapSections(){
  const main=document.querySelector('main');
  if(!main||main.dataset.collapsibleDone)return;
  const nodesUl=document.getElementById('nodes');
  const headers=Array.from(main.children).filter(el=>el.tagName==='H2');
  const rolesHeader=headers.find(h=>h.textContent.trim()==='Client-Rollen');
  if(rolesHeader&&nodesUl&&nodesUl.parentElement===main){
    const after=rolesHeader.nextElementSibling;
    if(after&&after.tagName==='P') after.insertAdjacentElement('afterend',nodesUl);
    else rolesHeader.insertAdjacentElement('afterend',nodesUl);
  }
  const groups=[];let current=null;
  Array.from(main.children).forEach(el=>{
    if(el.tagName==='H2'){current={header:el,items:[]};groups.push(current)}
    else if(current){current.items.push(el)}
  });
  if(!groups.length)return;
  groups.forEach(g=>{
    const details=document.createElement('details');details.className='section';details.open=true;
    main.insertBefore(details,g.header);
    const summary=document.createElement('summary');summary.appendChild(g.header);
    details.appendChild(summary);
    g.items.forEach(item=>details.appendChild(item));
  });
  main.dataset.collapsibleDone='1';
}
setTimeout(wrapSections,800);setTimeout(wrapSections,2000);
})();</script>"""
            encoded = page.encode("utf-8")
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(encoded))); self.end_headers(); self.wfile.write(encoded)
            return
            page = '''<!doctype html><html lang="de"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>SeriesAI Dashboard</title><style>body{font-family:system-ui;background:#10151f;color:#edf2f7;margin:0;padding:2rem}main{max-width:980px;margin:auto}h1{margin-bottom:.2rem}.muted{color:#a7b5c9}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:1rem;margin:1.5rem 0}.card,li{background:#182130;border:1px solid #2d3b52;border-radius:12px;padding:1rem}.value{font-size:1.8rem;color:#69d4ff;font-weight:700}h2{margin-top:2rem;font-size:1rem}ul{padding:0;display:grid;gap:.6rem;list-style:none}select{background:#10151f;color:#edf2f7;border:1px solid #4b6385;border-radius:6px;padding:.35rem;margin-left:.5rem}code{color:#9ee493}</style><main><h1>SeriesAI Dashboard</h1><p class="muted">Master &amp; Coordinator · automatische Aktualisierung alle 3 Sekunden</p><section class="grid"><div class="card"><small>Master-PID</small><div class="value" id="pid">–</div></div><div class="card"><small>Rechenknoten</small><div class="value" id="workers">–</div></div><div class="card"><small>Episoden</small><div class="value" id="episodes">–</div></div><div class="card"><small>Audio fertig</small><div class="value" id="audio">–</div></div><div class="card"><small>Szenen fertig</small><div class="value" id="scenes">–</div></div><div class="card"><small>Transkripte fertig</small><div class="value" id="transcripts">–</div></div><div class="card"><small>Transkript-ETA</small><div class="value" id="eta">–</div></div></section><h2>Aktuelle Schritte</h2><ul id="active"><li class="muted">Lade Status …</li></ul><h2>Client-Rollen</h2><p class="muted">Nur der Master kann Koordinator sein. Rollen steuern die spaetere lokale Aufgabenverteilung; sie starten oder stoppen keine bereits laufenden Jobs.</p><ul id="nodes"></ul><p class="muted">Alles lokal. API: <code>/status</code></p></main><script>const roles=['coordinator-only','coordinator-worker','worker'];function dur(s){if(s==null)return 'wird berechnet';let h=Math.floor(s/3600),m=Math.floor(s%3600/60);return h+'h '+m+'m'}async function setRole(id,role){let r=await fetch('/workers/'+id+'/role',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({role})});if(!r.ok)alert((await r.json()).error);refresh()}async function refresh(){let s=await fetch('/status').then(r=>r.json()),p=s.pipeline||{};pid.textContent=s.pid;workers.textContent=s.workers;episodes.textContent=p.episodes??'–';audio.textContent=p.audio??'–';scenes.textContent=p.scenes??'–';transcripts.textContent=p.transcripts??'–';eta.textContent=dur(p.transcription_eta_seconds);active.innerHTML=(p.active?.length?p.active.map(x=>'<li><b>'+x.stage+'</b> · '+x.episode_id+' · seit '+x.started_at+'</li>'):['<li class="muted">Keine offenen Datenbankschritte.</li>']).join('');nodes.innerHTML=s.worker_nodes.map(x=>'<li><b>'+x.name+'</b> · '+x.platform+'<label> Rolle <select onchange="setRole(\''+x.id+'\',this.value)">'+roles.filter(role=>x.id!=='master'||role!=='worker').map(role=>'<option '+(role===x.role?'selected':'')+' value="'+role+'">'+role+'</option>').join('')+'</select></label></li>').join('')}refresh();setInterval(refresh,3000)</script></html>'''.encode("utf-8")
            page = page.decode("utf-8").replace("</main>", '''<h2>Service-Steuerung</h2><section class="grid"><div class="card"><small>Code nach Aenderungen neu laden</small><p><button id="restart" onclick="restartCoordinator()">Koordinator neu starten</button></p><p class="muted" id="restartStatus">Startet nur den Koordinator neu. Laufende Jobs bleiben unveraendert.</p></div><div class="card"><small>Letzte Dashboard-Aktualisierung</small><div class="value" id="updated">–</div></div></section></main>''').replace("</script>", '''async function restartCoordinator(){let b=document.getElementById('restart'),m=document.getElementById('restartStatus');b.disabled=true;m.textContent='Neustart wird ausgefuehrt ...';try{let r=await fetch('/control/restart',{method:'POST'});if(!r.ok)throw Error();setTimeout(()=>{m.textContent='Koordinator neu geladen.';b.disabled=false;refresh()},1200)}catch(e){m.textContent='Koordinator nicht erreichbar.';b.disabled=false}}let originalRefresh=refresh;refresh=async function(){try{await originalRefresh();updated.textContent=new Date().toLocaleTimeString('de-DE')}catch(e){updated.textContent='OFFLINE';document.getElementById('restartStatus').textContent='Koordinator ist gestoppt oder nicht erreichbar.'}};</script>''').encode("utf-8")
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(page))); self.end_headers(); self.wfile.write(page)
    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--host", default="127.0.0.1"); parser.add_argument("--port", default=8765, type=int); args = parser.parse_args()
    start_discovery_responder(args.host, args.port)
    ThreadingHTTPServer((args.host, args.port), handler_factory(Coordinator(args.host, args.port))).serve_forever()


if __name__ == "__main__": main()
