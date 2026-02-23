from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class TemplateStore:
    def __init__(self, root_path: str) -> None:
        self.root = Path(root_path)
        self.root.mkdir(parents=True, exist_ok=True)

    def load_household_templates(
        self, household_id: str, document_type: str | None = None
    ) -> list[dict[str, Any]]:
        folder = self.root / household_id
        if not folder.exists():
            return []
        templates: list[dict[str, Any]] = []
        for file in folder.glob("*.json"):
            try:
                data = json.loads(file.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            if document_type and data.get("document_type") != document_type:
                continue
            templates.append(data)
        return templates

    def get_template(self, household_id: str, template_family_id: str) -> dict[str, Any] | None:
        path = self.root / household_id / f"{template_family_id}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def save_template(self, template: dict[str, Any]) -> Path:
        household_id = str(template["household_id"])
        family_id = str(template["template_family_id"])
        folder = self.root / household_id
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{family_id}.json"
        path.write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

