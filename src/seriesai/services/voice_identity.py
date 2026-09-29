"""Recognize the original voices: speaker embeddings, clusters, reviewed character mapping, TTS references.

Segments of every completed transcript are embedded locally (SpeechBrain ECAPA),
grouped into voice clusters, and only *reviewed* clusters become a character's
voice.  Nothing is assigned automatically: a cluster that is not approved for a
character never produces reference audio.
"""
from __future__ import annotations

import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..config import AppConfig
from ..db import Database
from ..paths import project_path, project_relative

MIN_SECONDS, MAX_SECONDS = 1.5, 12.0
SAMPLE_RATE = 16000
EMBEDDING_DIM = 192


@dataclass(frozen=True)
class EmbedResult:
    episode_id: str
    embedded: int
    status: str


def _normalize(vectors: np.ndarray) -> np.ndarray:
    return vectors / np.maximum(np.linalg.norm(vectors, axis=-1, keepdims=True), 1e-9)


class VoiceIdentityService:
    def __init__(self, config: AppConfig, database: Database) -> None:
        self.config, self.database = config, database
        self._encoder = None

    # -- embeddings ---------------------------------------------------------
    def _load_encoder(self):
        if self._encoder is None:
            import torch
            from speechbrain.inference.speaker import EncoderClassifier
            from speechbrain.utils.fetching import LocalStrategy
            source = self.config.paths.project_root / "models" / "speechbrain" / "spkrec-ecapa-voxceleb"
            device = "cuda" if torch.cuda.is_available() and torch.cuda.mem_get_info()[0] > 2 * 1024 ** 3 else "cpu"
            self._encoder = EncoderClassifier.from_hparams(
                source=str(source), savedir=str(source), run_opts={"device": device},
                overrides={"pretrained_path": str(source)}, local_strategy=LocalStrategy.COPY)
        return self._encoder

    def embed_episode(self, episode_id: str) -> EmbedResult:
        import soundfile
        import torch
        with self.database.connect() as connection:
            audio = connection.execute("SELECT file_path FROM import_artifacts WHERE episode_id=? AND artifact_type='audio_16khz_mono' AND status='completed'", (episode_id,)).fetchone()
            segments = [dict(row) for row in connection.execute(
                "SELECT s.segment_id,s.start_seconds,s.end_seconds FROM transcript_segments s JOIN transcripts t ON t.transcript_id=s.transcript_id "
                "LEFT JOIN voice_embeddings v ON v.segment_id=s.segment_id WHERE t.episode_id=? AND t.status='completed' AND v.segment_id IS NULL "
                "AND s.end_seconds-s.start_seconds BETWEEN ? AND ? AND COALESCE(s.no_speech_probability,0)<0.5 ORDER BY s.start_seconds", (episode_id, MIN_SECONDS, MAX_SECONDS))]
        if not audio: raise ValueError("Imported 16 kHz audio is required.")
        if not segments: return EmbedResult(episode_id, 0, "already_embedded")
        wave, rate = soundfile.read(str(project_path(self.config.paths.project_root, audio["file_path"])), dtype="float32")
        if rate != SAMPLE_RATE: raise ValueError(f"Expected {SAMPLE_RATE} Hz audio, got {rate}.")
        encoder, rows = self._load_encoder(), []
        for segment in segments:
            clip = wave[int(segment["start_seconds"] * rate):int(segment["end_seconds"] * rate)]
            with torch.no_grad():
                vector = encoder.encode_batch(torch.from_numpy(clip).unsqueeze(0)).squeeze().cpu().numpy()
            rows.append((segment["segment_id"], episode_id, segment["start_seconds"], segment["end_seconds"], _normalize(vector).astype(np.float32).tobytes()))
        with self.database.connect() as connection:
            connection.executemany("INSERT OR IGNORE INTO voice_embeddings(segment_id,episode_id,start_seconds,end_seconds,embedding) VALUES(?,?,?,?,?)", rows)
        return EmbedResult(episode_id, len(rows), "completed")

    def embed_series(self, series_id: str, progress=None) -> list[EmbedResult]:
        with self.database.connect() as connection:
            ids = [row[0] for row in connection.execute("SELECT e.episode_id FROM episodes e JOIN transcripts t ON t.episode_id=e.episode_id AND t.status='completed' WHERE e.series_id=? ORDER BY e.season_number,e.episode_number", (series_id,))]
        results = []
        for index, episode_id in enumerate(ids, start=1):
            try: result = self.embed_episode(episode_id)
            except Exception as error: result = EmbedResult(episode_id, 0, f"failed: {error}")
            results.append(result)
            if progress: progress(index, len(ids), result)
        return results

    # -- clustering ---------------------------------------------------------
    def cluster_series(self, series_id: str, force: bool = False) -> dict:
        from sklearn.cluster import AgglomerativeClustering
        with self.database.connect() as connection:
            if not force and connection.execute("SELECT 1 FROM voice_clusters WHERE series_id=? AND status='approved'", (series_id,)).fetchone():
                raise ValueError("Approved voice clusters exist; re-clustering would break the mapping. Use --force to replace all clusters.")
            rows = connection.execute("SELECT v.segment_id,v.episode_id,v.start_seconds,v.end_seconds,v.embedding FROM voice_embeddings v JOIN episodes e ON e.episode_id=v.episode_id WHERE e.series_id=?", (series_id,)).fetchall()
        if len(rows) < 20: raise ValueError("Not enough embedded speech; run embed-voices first.")
        vectors = _normalize(np.stack([np.frombuffer(row["embedding"], dtype=np.float32) for row in rows]))
        episodes = np.array([row["episode_id"] for row in rows])
        # Stage 1: local clusters per episode (cosine distance, average linkage).
        local_centroids: list[np.ndarray] = []; local_members: list[np.ndarray] = []
        for episode in np.unique(episodes):
            indices = np.where(episodes == episode)[0]
            if len(indices) < 10: continue
            labels = AgglomerativeClustering(n_clusters=None, metric="cosine", linkage="average", distance_threshold=0.6).fit_predict(vectors[indices])
            for label in np.unique(labels):
                members = indices[labels == label]
                if len(members) >= 8:
                    local_centroids.append(_normalize(vectors[members].mean(axis=0))); local_members.append(members)
        if len(local_centroids) < 2: raise ValueError("Too little consistent speech to form voice clusters.")
        # Stage 2: merge the episode-local voices across the series.
        centroids = np.stack(local_centroids)
        global_labels = AgglomerativeClustering(n_clusters=None, metric="cosine", linkage="average", distance_threshold=0.45).fit_predict(centroids)
        global_centroids = np.stack([_normalize(centroids[global_labels == label].mean(axis=0)) for label in np.unique(global_labels)])
        # Assign every segment to its nearest global voice when it is close enough.
        similarity = vectors @ global_centroids.T
        assignment = np.where(similarity.max(axis=1) >= 0.35, similarity.argmax(axis=1), -1)
        durations = np.array([row["end_seconds"] - row["start_seconds"] for row in rows])
        order = sorted((label for label in range(len(global_centroids)) if (assignment == label).sum() >= 30), key=lambda label: -durations[assignment == label].sum())
        with self.database.connect() as connection:
            connection.execute("UPDATE voice_embeddings SET cluster_id=NULL WHERE episode_id IN (SELECT episode_id FROM episodes WHERE series_id=?)", (series_id,))
            connection.execute("DELETE FROM voice_clusters WHERE series_id=?", (series_id,))
            summary = []
            for rank, label in enumerate(order, start=1):
                cluster_id = f"vc_{rank:03d}"; mask = assignment == label
                connection.execute("INSERT INTO voice_clusters(cluster_id,series_id,segment_count,total_seconds,centroid) VALUES(?,?,?,?,?)", (cluster_id, series_id, int(mask.sum()), float(durations[mask].sum()), global_centroids[label].astype(np.float32).tobytes()))
                connection.executemany("UPDATE voice_embeddings SET cluster_id=? WHERE segment_id=?", [(cluster_id, rows[i]["segment_id"]) for i in np.where(mask)[0]])
                summary.append({"cluster_id": cluster_id, "segments": int(mask.sum()), "minutes": round(float(durations[mask].sum()) / 60, 1)})
        return {"clusters": summary, "unassigned_segments": int((assignment == -1).sum()) + int(sum((assignment == label).sum() for label in range(len(global_centroids)) if label not in order))}

    # -- review -------------------------------------------------------------
    def _best_segments(self, cluster_id: str, count: int, min_seconds: float = 4.0, max_seconds: float = 12.0) -> list[dict]:
        with self.database.connect() as connection:
            centroid = connection.execute("SELECT centroid FROM voice_clusters WHERE cluster_id=?", (cluster_id,)).fetchone()
            rows = connection.execute("SELECT segment_id,episode_id,start_seconds,end_seconds,embedding FROM voice_embeddings WHERE cluster_id=? AND end_seconds-start_seconds BETWEEN ? AND ?", (cluster_id, min_seconds, max_seconds)).fetchall()
        if not centroid: raise ValueError(f"Unknown voice cluster: {cluster_id}")
        centre = np.frombuffer(centroid["centroid"], dtype=np.float32)
        scored = sorted(((float(np.frombuffer(row["embedding"], dtype=np.float32) @ centre), dict(row)) for row in rows), key=lambda item: -item[0])
        chosen: list[dict] = []
        for score, row in scored:   # closest to the voice centre, spread across episodes
            if sum(1 for item in chosen if item["episode_id"] == row["episode_id"]) < 2:
                row.pop("embedding"); row["similarity"] = score; chosen.append(row)
            if len(chosen) == count: break
        return chosen

    def _cut(self, episode_id: str, start: float, end: float, target: Path, sample_rate: int) -> None:
        if target.is_file() and target.stat().st_size > 0: return   # idempotent: reused by every dashboard poll
        with self.database.connect() as connection:
            audio = connection.execute("SELECT file_path FROM import_artifacts WHERE episode_id=? AND artifact_type='audio_16khz_mono' AND status='completed'", (episode_id,)).fetchone()
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".partial" + target.suffix)
        subprocess.run([self.config.paths.ffmpeg, "-y", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(project_path(self.config.paths.project_root, audio["file_path"])), "-ac", "1", "-ar", str(sample_rate), str(temporary)], check=True, capture_output=True, timeout=120)
        temporary.replace(target)

    def review_clusters(self, series_id: str, samples: int = 4) -> list[dict]:
        """List clusters with listening samples (Series/voice_review/<cluster>/). Sample files are cut once (idempotent) and reused on every later call."""
        with self.database.connect() as connection:
            clusters = [dict(row) for row in connection.execute("SELECT cluster_id,character_id,segment_count,total_seconds,status FROM voice_clusters WHERE series_id=? ORDER BY cluster_id", (series_id,))]
        for cluster in clusters:
            cluster["sample_files"], cluster["sample_texts"] = [], []
            for index, segment in enumerate(self._best_segments(cluster["cluster_id"], samples), start=1):
                target = self.config.paths.series_root / "voice_review" / cluster["cluster_id"] / f"sample_{index}.wav"
                self._cut(segment["episode_id"], segment["start_seconds"], segment["end_seconds"], target, SAMPLE_RATE)
                cluster["sample_files"].append(project_relative(self.config.paths.project_root, target))
                with self.database.connect() as connection:
                    cluster["sample_texts"].append(connection.execute("SELECT text FROM transcript_segments WHERE segment_id=?", (segment["segment_id"],)).fetchone()[0])
        self._write_review_page(series_id, clusters)
        return clusters

    def _write_review_page(self, series_id: str, clusters: list[dict]) -> None:
        """Series/voice_review/index.html: listen to each cluster and copy the assign command."""
        import html
        with self.database.connect() as connection:
            characters = [row[0] for row in connection.execute("SELECT character_id FROM characters WHERE series_id=? AND active=1 ORDER BY canonical_name", (series_id,))]
        blocks = []
        for cluster in sorted(clusters, key=lambda item: -item["total_seconds"]):
            players = "".join(f'<li><audio controls preload="none" src="{html.escape(Path(f).relative_to(self.config.paths.project_root / "Series" / "voice_review").as_posix() if Path(f).is_absolute() else Path(f).relative_to("Series/voice_review").as_posix())}"></audio> <small>{html.escape(text)}</small></li>' for f, text in zip(cluster["sample_files"], cluster["sample_texts"]))
            blocks.append(f'<section><h2>{cluster["cluster_id"]} <small>{cluster["segment_count"]} Segmente, {cluster["total_seconds"] / 60:.0f} min, Status {cluster["status"]}</small></h2><ul>{players}</ul>'
                          f'<code>python -m seriesai.cli assign-voice --cluster-id {cluster["cluster_id"]} --character-id &lt;{" | ".join(characters)}&gt;</code></section>')
        page = ("<!doctype html><meta charset=utf-8><title>Stimmen-Pruefung</title><style>body{font:15px system-ui;background:#10151f;color:#edf2f7;max-width:900px;margin:2rem auto;padding:0 1rem}"
                "section{background:#182130;border:1px solid #2d3b52;border-radius:10px;padding:1rem;margin:1rem 0}small{color:#a7b5c9}code{display:block;margin-top:.6rem;color:#9ee493}li{margin:.4rem 0;list-style:none}</style>"
                "<h1>Stimmen-Pruefung</h1><p>Jeder Block ist eine erkannte Stimme. Anhoeren, dann die Figur mit <b>assign-voice</b> zuordnen; Misch-, Gast- oder Lachspur-Cluster mit <b>reject-voice</b> ablehnen.</p>" + "".join(blocks))
        (self.config.paths.series_root / "voice_review" / "index.html").write_text(page, encoding="utf-8")

    def assign_cluster(self, cluster_id: str, character_id: str) -> dict:
        with self.database.connect() as connection:
            if not connection.execute("SELECT 1 FROM characters WHERE character_id=? AND active=1", (character_id,)).fetchone(): raise ValueError(f"Unknown approved character: {character_id}")
            if connection.execute("SELECT 1 FROM voice_clusters WHERE character_id=? AND cluster_id<>?", (character_id, cluster_id)).fetchone(): raise ValueError("This character already has another voice cluster; reject it first.")
            changed = connection.execute("UPDATE voice_clusters SET character_id=?,status='approved' WHERE cluster_id=?", (character_id, cluster_id)).rowcount
        if not changed: raise ValueError(f"Unknown voice cluster: {cluster_id}")
        return {"cluster_id": cluster_id, "character_id": character_id, "status": "approved"}

    def reject_cluster(self, cluster_id: str) -> dict:
        with self.database.connect() as connection:
            connection.execute("UPDATE voice_clusters SET character_id=NULL,status='rejected' WHERE cluster_id=?", (cluster_id,))
        return {"cluster_id": cluster_id, "status": "rejected"}

    # -- references for the TTS backend --------------------------------------
    def build_references(self, series_id: str, clips: int = 6) -> list[dict]:
        """Cut the cleanest clips (closest to the voice centre) of each approved character."""
        with self.database.connect() as connection:
            approved = [dict(row) for row in connection.execute("SELECT cluster_id,character_id FROM voice_clusters WHERE series_id=? AND status='approved' AND character_id IS NOT NULL", (series_id,))]
        results = []
        for cluster in approved:
            target_dir = self.config.paths.series_root / "voices" / cluster["character_id"]
            for stale in target_dir.glob("ref_*.wav"): stale.unlink()
            segments = self._best_segments(cluster["cluster_id"], clips, min_seconds=4.0, max_seconds=11.0)
            for index, segment in enumerate(segments, start=1):
                self._cut(segment["episode_id"], segment["start_seconds"], segment["end_seconds"], target_dir / f"ref_{index:02d}.wav", 22050)
            with self.database.connect() as connection:
                connection.execute(
                    "INSERT INTO voice_profiles(voice_profile_id,character_id,voice_identity,backend_name,reference_dir) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(character_id) DO UPDATE SET backend_name=excluded.backend_name,reference_dir=excluded.reference_dir",
                    (str(uuid.uuid4()), cluster["character_id"], cluster["character_id"], "xtts", project_relative(self.config.paths.project_root, target_dir)))
            results.append({"character_id": cluster["character_id"], "cluster_id": cluster["cluster_id"], "reference_clips": len(segments), "reference_dir": str(target_dir)})
        return results
