from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class TaskStatus(str, Enum):
    running = "running"
    needs_info = "needs_info"
    needs_confirmation = "needs_confirmation"
    needs_human = "needs_human"
    completed = "completed"
    failed = "failed"


class UserProfile(BaseModel):
    name: str = ""
    email: str = ""
    phone: str = ""


class TaskCreate(BaseModel):
    goal: str = Field(min_length=1, max_length=4000)
    start_url: Optional[str] = None
    profile: UserProfile = Field(default_factory=UserProfile)

    @field_validator("start_url")
    @classmethod
    def validate_start_url(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not value.strip():
            return None
        value = value.strip()
        if not (value.startswith("http://") or value.startswith("https://")):
            raise ValueError("start_url must be http:// or https://")
        return value


class ResumeRequest(BaseModel):
    answers: dict[str, Any] = Field(default_factory=dict)
    confirmed: Optional[bool] = None
    human_done: bool = False
    message: str = ""


class Observation(BaseModel):
    url: str = ""
    title: str = ""
    refs_text: str = ""
    page_text: str = ""
    error: Optional[str] = None

    def as_prompt(self) -> str:
        parts = [
            f"URL: {self.url}",
            f"Title: {self.title}",
            "Interactive elements (use only these numbered refs):",
            self.refs_text or "(none)",
        ]
        if self.page_text:
            parts.extend(["Visible text excerpt:", self.page_text])
        if self.error:
            parts.append(f"Error: {self.error}")
        return "\n".join(parts)


class RefInfo(BaseModel):
    ref: int
    tag: str = ""
    role: str = ""
    name: str = ""
    input_type: str = ""
    value: str = ""


class ToolResult(BaseModel):
    kind: Literal["observation", "pause", "complete", "fail"]
    observation: Optional[Observation] = None
    pause_status: Optional[TaskStatus] = None
    pause_payload: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
    evidence: str = ""
    reason: str = ""


class NavigateArgs(BaseModel):
    url: str = Field(min_length=1, max_length=2000)

    @field_validator("url")
    @classmethod
    def http_url(cls, value: str) -> str:
        value = value.strip()
        if not (value.startswith("http://") or value.startswith("https://")):
            raise ValueError("url must start with http:// or https://")
        return value


class RefArgs(BaseModel):
    ref: int = Field(ge=1)


class TypeArgs(BaseModel):
    ref: int = Field(ge=1)
    text: str = Field(min_length=0, max_length=2000)


class SelectArgs(BaseModel):
    ref: int = Field(ge=1)
    value: str = Field(min_length=1, max_length=500)


class PressKeyArgs(BaseModel):
    key: str = Field(min_length=1, max_length=40)


class AskUserArgs(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    fields: list[str] = Field(default_factory=list)


class ConfirmationArgs(BaseModel):
    summary: str = Field(min_length=1, max_length=2000)
    action: str = Field(min_length=1, max_length=500)
    ref: Optional[int] = Field(default=None, ge=1)


class HumanArgs(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class CompleteArgs(BaseModel):
    summary: str = Field(min_length=1, max_length=4000)
    evidence: str = Field(min_length=1, max_length=4000)


class FailArgs(BaseModel):
    reason: str = Field(min_length=1, max_length=4000)
