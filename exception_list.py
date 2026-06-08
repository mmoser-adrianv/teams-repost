from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ExceptionList:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def list_emails(self) -> list[str]:
        return sorted(self._load().get("emails", []))

    def email_set(self) -> set[str]:
        return set(self.list_emails())

    def contains(self, email: str | None) -> bool:
        normalized = normalize_email(email)
        return bool(normalized and normalized in self.email_set())

    def add(self, email: str) -> list[str]:
        normalized = normalize_email(email)
        if not normalized:
            raise ValueError("Enter a valid email address")

        data = self._load()
        emails = set(data.get("emails", []))
        emails.add(normalized)
        data["emails"] = sorted(emails)
        data["updated_at"] = datetime.now(UTC).isoformat()
        self._save(data)
        return list(data["emails"])

    def remove(self, email: str) -> list[str]:
        normalized = normalize_email(email)
        if not normalized:
            raise ValueError("Enter a valid email address")

        data = self._load()
        emails = set(data.get("emails", []))
        emails.discard(normalized)
        data["emails"] = sorted(emails)
        data["updated_at"] = datetime.now(UTC).isoformat()
        self._save(data)
        return list(data["emails"])

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"emails": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Exception list file is not valid JSON: {self.path}") from exc
        if isinstance(data, list):
            data = {"emails": data}
        if not isinstance(data, dict):
            raise ValueError(f"Exception list file must contain a JSON object: {self.path}")
        emails = data.get("emails")
        if emails is None:
            data["emails"] = []
        elif not isinstance(emails, list):
            raise ValueError(f"Exception list 'emails' must be an array: {self.path}")
        else:
            normalized = []
            for email in emails:
                if not isinstance(email, str):
                    raise ValueError(f"Exception list emails must be strings: {self.path}")
                value = normalize_email(email)
                if value:
                    normalized.append(value)
            data["emails"] = sorted(set(normalized))
        return data

    def _save(self, data: dict[str, Any]) -> None:
        temp_path = self.path.with_name(self.path.name + ".tmp")
        temp_path.write_text(json.dumps(data, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temp_path.replace(self.path)


def normalize_email(email: str | None) -> str | None:
    if not email:
        return None
    normalized = email.strip().lower()
    return normalized if _EMAIL_RE.match(normalized) else None
