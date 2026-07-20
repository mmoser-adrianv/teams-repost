from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from .models import ResourceCatalogue


class ResourceCatalogueState:
    def __init__(self, path: Path) -> None:
        self.path = path

    def record(self, catalogue: ResourceCatalogue) -> bool:
        changed = self._previous_updated_at() != catalogue.updated_at
        if not changed:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"{self.path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(
                    {
                        "schema_version": catalogue.schema_version,
                        "updated_at": catalogue.updated_at,
                        "resource_count": catalogue.resource_count,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)
        return True

    def _previous_updated_at(self) -> str | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        updated_at = payload.get("updated_at") if isinstance(payload, dict) else None
        return updated_at if isinstance(updated_at, str) else None
