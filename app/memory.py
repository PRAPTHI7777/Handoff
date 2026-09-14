"""Small explicit-only persistent memory for a single local Handoff user."""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4


SENSITIVE_RE = re.compile(
    r"\b(api[ _-]?key|secret|access[ _-]?token|password|passcode|"
    r"one[ _-]?time[ _-]?password|\botp\b|cvv|credit[ _-]?card|ssn)\b",
    re.IGNORECASE,
)


class MemoryStoreError(ValueError):
    pass


class MemoryStore:
    """JSON-backed memory. Values are saved only through ``save`` calls."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def save(self, value: str) -> Dict[str, str]:
        value = _clean_value(value)
        data = self._read()
        for item in data["memories"]:
            if item["value"].casefold() == value.casefold():
                return item

        item = {
            "id": uuid4().hex[:12],
            "value": value,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        data["memories"].append(item)
        self._write(data)
        return item

    def relevant(self, query: str, limit: int = 3) -> List[Dict[str, str]]:
        data = self._read()
        query_words = _keywords(query)
        ranked = []
        for item in data["memories"]:
            value = item["value"]
            if SENSITIVE_RE.search(value):
                continue
            score = len(query_words & _keywords(value))
            if score:
                ranked.append((score, item))
        ranked.sort(key=lambda pair: (pair[0], pair[1]["created_at"]), reverse=True)
        return [item for _, item in ranked[:max(1, min(limit, 5))]]

    def get(self, memory_id: str) -> Optional[Dict[str, str]]:
        for item in self._read()["memories"]:
            if item["id"] == memory_id:
                return item
        return None

    def delete(self, memory_id: str) -> bool:
        data = self._read()
        remaining = [
            item for item in data["memories"]
            if item["id"] != memory_id
        ]
        if len(remaining) == len(data["memories"]):
            return False
        data["memories"] = remaining
        self._write(data)
        return True

    def _read(self) -> Dict[str, List[Dict[str, str]]]:
        if not self.path.exists():
            return {"memories": []}
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                raw: Any = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise MemoryStoreError("Could not read local memory store") from exc

        memories = raw.get("memories") if isinstance(raw, dict) else None
        if not isinstance(memories, list):
            raise MemoryStoreError("Local memory store has an invalid format")

        clean = []
        for item in memories:
            if not isinstance(item, dict):
                continue
            memory_id = item.get("id")
            value = item.get("value")
            created_at = item.get("created_at")
            if (
                isinstance(memory_id, str)
                and isinstance(value, str)
                and isinstance(created_at, str)
                and not SENSITIVE_RE.search(value)
            ):
                clean.append(
                    {
                        "id": memory_id,
                        "value": value,
                        "created_at": created_at,
                    }
                )
        return {"memories": clean}

    def _write(self, data: Dict[str, List[Dict[str, str]]]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temporary, self.path)
        except OSError as exc:
            raise MemoryStoreError("Could not write local memory store") from exc


def _clean_value(value: str) -> str:
    value = " ".join(str(value).replace("\x00", " ").split())
    if not value or len(value) > 500:
        raise MemoryStoreError("Memory must be between 1 and 500 characters")
    if SENSITIVE_RE.search(value):
        raise MemoryStoreError("Sensitive credentials and payment data cannot be stored as memory")
    return value


def _keywords(text: str) -> set:
    return {
        word.casefold()
        for word in re.findall(r"[A-Za-z0-9]{3,}", text)
    }
