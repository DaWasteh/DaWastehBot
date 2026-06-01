"""Tests für PandaBot.

Decken die reine Logik ab (Sanitizing der LLM-Antworten), die ohne echte
Twitch- oder LLM-Verbindung getestet werden kann.
"""

from __future__ import annotations

import pytest

from pandabot import LLMClient


@pytest.fixture
def client() -> LLMClient:
    return LLMClient()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("PandaBot: Hallo zusammen!", "Hallo zusammen!"),
        ('"Na klar, viel Spaß!"', "Na klar, viel Spaß!"),
        ("  Bot: test  ", "test"),
        ("Normale Antwort", "Normale Antwort"),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_sanitize(client: LLMClient, raw: str | None, expected: str | None) -> None:
    assert client._sanitize(raw) == expected


def test_sanitize_truncates_long_text(client: LLMClient) -> None:
    out = client._sanitize("A" * 600)
    assert out is not None
    assert len(out) <= 480
    assert out.endswith("…")


def test_sanitize_strips_own_name_case_insensitive(client: LLMClient) -> None:
    assert client._sanitize("pandabot: yo") == "yo"
