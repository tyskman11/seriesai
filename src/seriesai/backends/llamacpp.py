"""Local-only llama.cpp backend (llama-server, OpenAI-compatible API) with validated structured output."""
from __future__ import annotations

import json
from typing import Any
from urllib.request import Request, urlopen

from pydantic import BaseModel, ValidationError

from .base import LLMBackend


class LlamaCppBackend(LLMBackend):
    def __init__(self, base_url: str, model: str = "local", retries: int = 2, timeout_seconds: int = 1200) -> None:
        # A large default: under GPU contention with a concurrent ComfyUI render, llama.cpp
        # can fall back to slow CPU decoding for a multi-billion-parameter model.
        self.base_url, self.model, self.retries, self.timeout_seconds = base_url.rstrip("/"), model, retries, timeout_seconds

    def generate(self, prompt: str, *, system: str | None = None) -> str:
        return self._request(self._payload(prompt, system))["response"]

    @staticmethod
    def _payload(prompt: str, system: str | None, **extra: Any) -> dict[str, Any]:
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        # enable_thinking=False: Qwen3-style templates otherwise emit a long <think> block first.
        return {"messages": messages, "stream": False, "chat_template_kwargs": {"enable_thinking": False}, **extra}

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST to llama-server; returns {"response": <assistant text>} (the shape the callers expect)."""
        request = Request(f"{self.base_url}/v1/chat/completions", data=json.dumps({"model": self.model, **payload}).encode(), headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=self.timeout_seconds) as response:
            return {"response": json.loads(response.read())["choices"][0]["message"]["content"] or ""}

    def structured_generate(self, prompt: str, schema: type[Any], *, system: str | None = None) -> Any:
        if not issubclass(schema, BaseModel):
            raise TypeError("Structured generation requires a Pydantic schema.")
        # Keep the schema in the request, but do not invite the model to repeat
        # it. Smaller local models otherwise occasionally return the schema
        # itself instead of an instance of it.
        json_schema = schema.model_json_schema()
        instruction = ("Gib ausschliesslich ein einzelnes JSON-Objekt aus, ohne Markdown oder Erklaerung. "
                       "Halte exakt dieses Schema ein; erfinde keine alternativen Feldnamen. Pflichtfelder: "
                       + ", ".join(json_schema.get("required", [])))
        last_error: Exception | None = None
        for _ in range(self.retries + 1):
            try:
                payload = self._payload(prompt + "\n\n" + instruction, system, temperature=0.15,
                                        response_format={"type": "json_schema", "json_schema": {"name": schema.__name__, "schema": json_schema}})
                response = self._request(payload)["response"].strip()
                if response.startswith("```"):
                    response = response.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
                return schema.model_validate_json(response)
            except (json.JSONDecodeError, ValidationError, KeyError) as error: last_error = error
        raise RuntimeError(f"Invalid llama.cpp JSON after {self.retries + 1} attempts: {last_error}")

    def summarize(self, text: str) -> str: return self.generate("Fasse auf Deutsch zusammen:\n" + text)

    def extract_events(self, text: str) -> list[dict[str, Any]]:
        """Return conservative, local structured event candidates.

        This adapter does not write to SQLite; callers remain responsible for
        evidence review and canonical updates.
        """
        class EventBatch(BaseModel):
            events: list[dict[str, Any]]
        result = self.structured_generate(
            "Extrahiere nur explizit belegte Serienereignisse aus diesem deutschen Text. "
            "Jedes Ereignis braucht mindestens description und event_type. Keine Vermutungen.\n" + text,
            EventBatch,
            system="Du extrahierst konservative, belegbare Daten fuer eine Serienchronik.",
        )
        return result.events

    def check_continuity(self, context: dict[str, Any]) -> list[dict[str, Any]]:
        """Return reviewable issue candidates; deterministic checks stay primary."""
        class IssueBatch(BaseModel):
            issues: list[dict[str, Any]]
        result = self.structured_generate(
            "Pruefe diesen Serienentwurf gegen seinen Kontext auf Kontinuitaetsprobleme. "
            "Jeder Eintrag braucht severity, description und evidence. Wenn nichts belegt ist, gib [].\n"
            + json.dumps(context, ensure_ascii=False),
            IssueBatch,
            system="Du bist ein konservativer deutscher Continuity Editor. Erfinde keine Fakten.",
        )
        return result.issues
