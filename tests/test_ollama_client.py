from __future__ import annotations

import json
from typing import Any

from agentic_core.ollama_client import OllamaClient


class FakeResponse:
    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def read(self) -> bytes:
        return b'{"message":{"content":"{}"}}'


class FakeOpener:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    def open(self, request: Any, timeout: float) -> FakeResponse:
        self.payloads.append(json.loads(request.data.decode("utf-8")))
        return FakeResponse()


def test_ollama_client_can_request_json_format() -> None:
    opener = FakeOpener()
    client = OllamaClient()
    client._opener = opener  # type: ignore[assignment]

    client.chat([{"role": "user", "content": "hi"}], format_json=True)

    assert opener.payloads[0]["format"] == "json"


def test_ollama_client_does_not_request_json_format_by_default() -> None:
    opener = FakeOpener()
    client = OllamaClient()
    client._opener = opener  # type: ignore[assignment]

    client.chat([{"role": "user", "content": "hi"}])

    assert "format" not in opener.payloads[0]
