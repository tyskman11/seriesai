"""Build portable character-only LoRA dataset manifests from reviewed references."""
from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

from ..config import AppConfig
from ..db import Database
from ..paths import project_path, project_relative


class CharacterDatasetService:
    def __init__(self, config: AppConfig, database: Database) -> None:
        self.config, self.database = config, database

    def build(self, character_id: str) -> dict:
        with self.database.connect() as connection:
            character = connection.execute("SELECT character_id,series_id,canonical_name FROM characters WHERE character_id=? AND active=1", (character_id,)).fetchone()
            if not character: raise ValueError("Approved canonical character not found.")
            candidate = connection.execute("SELECT candidate_id FROM character_candidates WHERE canonical_name=? AND status='approved' LIMIT 1", (character[2],)).fetchone()
            refs = connection.execute("SELECT file_path,reference_kind,source_episode_id FROM visual_references WHERE entity_type='character_candidate' AND entity_id=? AND reference_kind IN ('review_face_crop','review_face') ORDER BY quality_score DESC", (candidate[0],)).fetchall() if candidate else []
            # collect-character-training-images adds many more identity-matched crops beyond
            # the single reviewed evidence frame above; both feed the same LoRA dataset.
            refs += connection.execute("SELECT file_path,reference_kind,source_episode_id FROM visual_references WHERE entity_type='character' AND entity_id=? AND reference_kind='training_crop' ORDER BY quality_score DESC", (character_id,)).fetchall()
        root = self.config.paths.series_root / "datasets" / character_id; images = root / "images"; captions = root / "captions"
        images.mkdir(parents=True, exist_ok=True); captions.mkdir(parents=True, exist_ok=True)
        entries=[]
        for index,row in enumerate(refs,1):
            source=project_path(self.config.paths.project_root,row[0])
            if not source.is_file(): continue
            target=images/f"{index:04d}{source.suffix.lower() or '.jpg'}"; shutil.copy2(source,target)
            caption=f"{character[2]}, character reference, {row[1]}"
            (captions/f"{index:04d}.txt").write_text(caption,encoding="utf-8")
            entries.append({"image":project_relative(self.config.paths.project_root,target),"caption":caption,"source_episode_id":row[2],"reference_kind":row[1]})
        manifest={"dataset_type":"character","character_id":character[0],"canonical_name":character[2],"style_mixed":False,"location_mixed":False,"entries":entries}
        output=root/"dataset.json"; output.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO training_datasets(dataset_id,series_id,dataset_type,entity_type,entity_id,manifest_path,status) VALUES(?,?,?,?,?,?,'ready') "
                "ON CONFLICT(dataset_id) DO UPDATE SET manifest_path=excluded.manifest_path,status=excluded.status",
                (f"character_lora_images:{character_id}", character[1], "character_lora_images", "character", character_id, str(output)),
            )
        return {"character_id":character_id,"image_count":len(entries),"manifest_path":str(output)}
