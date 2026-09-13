from abc import ABC, abstractmethod
from typing import Optional

from app.schemas import Observation, RefInfo


class BrowserSession(ABC):
    """Page control used by the agent. Implementations must keep the page open across pauses."""

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def navigate(self, url: str) -> Observation: ...

    @abstractmethod
    async def snapshot(self) -> Observation: ...

    @abstractmethod
    async def click(self, ref: int) -> Observation: ...

    @abstractmethod
    async def type_text(self, ref: int, text: str) -> Observation: ...

    @abstractmethod
    async def select(self, ref: int, value: str) -> Observation: ...

    @abstractmethod
    async def press_key(self, key: str) -> Observation: ...

    @abstractmethod
    async def screenshot_png(self) -> Optional[bytes]: ...

    @abstractmethod
    def ref_info(self, ref: int) -> Optional[RefInfo]: ...

    @abstractmethod
    def known_refs(self) -> set[int]: ...

    @abstractmethod
    def current_url(self) -> str: ...
