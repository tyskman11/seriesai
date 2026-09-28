"""Deterministic continuity checks for a generated draft before rendering."""
from __future__ import annotations

import json
from pathlib import Path


class ContinuityChecker:
    """Checks only facts that can be proven from the frozen draft context."""

    def check_document(self, outline_path: Path) -> dict:
        document = json.loads(outline_path.read_text(encoding="utf-8"))
        issues = self.check(document)
        report = {"outline_path": str(outline_path), "issue_count": len(issues), "issues": issues}
        report_path = outline_path.with_name("continuity_report.json")
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        document["continuity_issues"] = issues
        document["status"] = "needs_review" if issues else "continuity_checked"
        outline_path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
        return {**report, "report_path": str(report_path), "status": document["status"]}

    @staticmethod
    def check(document: dict) -> list[dict]:
        context = document.get("context_snapshot", {})
        outline = document.get("outline", {})
        characters = {item["character_id"]: item for item in context.get("characters", []) if item.get("character_id")}
        issues: list[dict] = []
        seen_numbers: set[int] = set()
        total = 0.0
        for scene in outline.get("scenes", []):
            number = scene.get("sequence_number")
            if number in seen_numbers:
                issues.append({"severity": "error", "rule": "duplicate_scene_number", "scene": number, "description": "Szenennummer kommt mehrfach vor."})
            seen_numbers.add(number)
            duration = scene.get("duration_seconds", 0)
            total += duration if isinstance(duration, (int, float)) else 0
            unknown = sorted(set(scene.get("character_ids", [])) - set(characters))
            if unknown:
                issues.append({"severity": "error", "rule": "unknown_character", "scene": number, "description": "Unbekannte feste Figuren-ID: " + ", ".join(unknown), "evidence": {"known_ids": sorted(characters)}})
            if not scene.get("location"):
                issues.append({"severity": "error", "rule": "missing_location", "scene": number, "description": "Szene hat keinen Ort."})
            if not scene.get("purpose"):
                issues.append({"severity": "warning", "rule": "missing_scene_purpose", "scene": number, "description": "Szene hat keinen erzählerischen Zweck."})
        target = document.get("target_duration_seconds")
        if isinstance(target, (int, float)) and abs(total - target) > max(90, target * .15):
            issues.append({"severity": "warning", "rule": "duration_mismatch", "description": f"Entwurf {round(total)}s statt Ziel {round(target)}s.", "evidence": {"planned_seconds": total, "target_seconds": target}})
        if not outline.get("scenes"):
            issues.append({"severity": "error", "rule": "no_scenes", "description": "Der Entwurf enthält keine Szenen."})
        return issues
