"""Explicitly managed local storage for the user's reusable profile."""

import json
import os
import re
from pathlib import Path
from typing import Any

from app.schemas import UserProfile


SENSITIVE_RE = re.compile(
    r"\b(api[ _-]?key|secret|access[ _-]?token|password|passcode|"
    r"one[ _-]?time[ _-]?password|\botp\b|cvv|credit[ _-]?card|"
    r"debit[ _-]?card|card(?:[ _-]?number)?|payment|"
    r"auth(?:entication)?[ _-]?token|bearer[ _-]?token|"
    r"ssn|security[ _-]?code)\b",
    re.IGNORECASE,
)


class ProfileStoreError(ValueError):
    pass


class ProfileStore:
    """JSON-backed profile; values are written only through explicit saves."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> UserProfile:
        if not self.path.exists():
            return UserProfile()
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                raw: Any = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ProfileStoreError("Could not read local profile store") from exc

        values = raw.get("profile") if isinstance(raw, dict) else None
        if not isinstance(values, dict):
            raise ProfileStoreError("Local profile store has an invalid format")
        try:
            return UserProfile.model_validate(values)
        except (TypeError, ValueError) as exc:
            raise ProfileStoreError("Local profile store has an invalid profile") from exc

    def save(self, profile: UserProfile) -> UserProfile:
        values = profile.model_dump()
        for value in values.values():
            if value and SENSITIVE_RE.search(value):
                raise ProfileStoreError(
                    "Passwords, OTPs, authentication secrets, and payment data "
                    "cannot be stored in the user profile"
                )

        data = {"profile": values}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            raise ProfileStoreError("Could not write local profile store") from exc
        return profile
