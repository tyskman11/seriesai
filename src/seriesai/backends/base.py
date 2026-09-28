from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class LLMBackend(ABC):
    @abstractmethod
    def generate(self, prompt: str, *, system: str | None = None) -> str: ...

    @abstractmethod
    def structured_generate(self, prompt: str, schema: type[Any], *, system: str | None = None) -> Any: ...

    @abstractmethod
    def summarize(self, text: str) -> str: ...

    @abstractmethod
    def extract_events(self, text: str) -> list[dict[str, Any]]: ...

    @abstractmethod
    def check_continuity(self, context: dict[str, Any]) -> list[dict[str, Any]]: ...


class TranscriptionBackend(ABC):
    @abstractmethod
    def transcribe(self, audio_path: str) -> dict[str, Any]: ...


class VisionBackend(ABC):
    @abstractmethod
    def analyze_frame(self, image_path: str) -> dict[str, Any]: ...


class VoiceBackend(ABC):
    @abstractmethod
    def synthesize(self, text: str, voice_identity: str, output_path: str, **options: Any) -> str: ...


class VideoBackend(ABC):
    @abstractmethod
    def render_shot(self, specification: dict[str, Any]) -> str: ...


class AudioBackend(ABC):
    @abstractmethod
    def assemble(self, specification: dict[str, Any]) -> str: ...
