"""Fixtures communes : tests 100 % hors-ligne (SQLite + FakeLLM, aucun réseau)."""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.agent import Agent  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import SQLiteDatabase  # noqa: E402

SQLITE_FILE = ROOT / "data" / "banque.db"


class FakeLLM:
    """LLM factice : renvoie des réponses scriptées et mémorise les messages reçus."""

    model = "fake-llm"

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    def chat(self, messages: list[dict], temperature: float = 0.0) -> str:
        self.calls.append([dict(m) for m in messages])
        if not self.replies:
            raise AssertionError("FakeLLM : plus de réponse scriptée disponible")
        return self.replies.pop(0)


def sql_block(sql: str) -> str:
    return f"```sql\n{sql}\n```"


@pytest.fixture(scope="session", autouse=True)
def _require_data():
    if not SQLITE_FILE.exists():
        pytest.exit("data/banque.db introuvable : lancez d'abord `python scripts/generate_data.py`.")


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    # Environnement neutre : les tests ne dépendent pas du .env du poste.
    for name in ("DB_BACKEND", "LLM_PROVIDER", "LLM_BASE_URL", "MAX_ROWS", "MAX_RETRIES", "SQLITE_PATH"):
        monkeypatch.delenv(name, raising=False)
    base = Settings.from_env()
    return dataclasses.replace(
        base, db_backend="sqlite", sqlite_path=SQLITE_FILE, audit_log=tmp_path / "audit.jsonl"
    )


@pytest.fixture
def db(settings) -> SQLiteDatabase:
    return SQLiteDatabase(settings)


@pytest.fixture
def make_agent(db, settings):
    def _make(replies: list[str]) -> tuple[Agent, FakeLLM]:
        llm = FakeLLM(replies)
        return Agent(db, llm, settings), llm

    return _make
