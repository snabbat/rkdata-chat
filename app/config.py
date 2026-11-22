"""Configuration de l'application, lue exclusivement dans les variables d'environnement.

Aucun secret n'est écrit dans le code : docker-compose.yml transmet les valeurs
du fichier .env (voir .env.example).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_LLM_URLS = {
    "ollama": "http://localhost:11434",
    "openai": "http://localhost:8000/v1",  # vLLM, LM Studio, llama.cpp server...
}


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


@dataclass(frozen=True)
class Settings:
    # --- Base de données
    db_backend: str  # "trino" ou "sqlite"
    trino_host: str
    trino_port: int
    trino_user: str
    trino_catalog: str
    trino_schema: str
    trino_http_scheme: str
    sqlite_path: Path
    # --- LLM
    llm_provider: str  # "ollama" ou "openai" (tout serveur compatible OpenAI)
    llm_base_url: str
    llm_model: str
    llm_api_key: str
    llm_timeout_s: int
    llm_num_ctx: int
    llm_keep_alive: str
    # --- Garde-fous
    max_rows: int
    query_timeout_s: int
    max_retries: int
    summary_rows: int
    history_turns: int
    # --- Divers
    semantic_file: Path
    audit_log: Path
    app_user: str

    @classmethod
    def from_env(cls) -> "Settings":
        db_backend = _env("DB_BACKEND", "sqlite").lower()
        if db_backend not in ("trino", "sqlite"):
            raise ValueError(f"DB_BACKEND invalide : {db_backend!r} (attendu : trino ou sqlite)")
        llm_provider = _env("LLM_PROVIDER", "ollama").lower()
        if llm_provider not in DEFAULT_LLM_URLS:
            raise ValueError(f"LLM_PROVIDER invalide : {llm_provider!r} (attendu : ollama ou openai)")
        return cls(
            db_backend=db_backend,
            trino_host=_env("TRINO_HOST", "localhost"),
            trino_port=int(_env("TRINO_PORT", "8080")),
            trino_user=_env("TRINO_USER", "chatbot"),
            trino_catalog=_env("TRINO_CATALOG", "iceberg"),
            trino_schema=_env("TRINO_SCHEMA", "banque"),
            trino_http_scheme=_env("TRINO_HTTP_SCHEME", "http"),
            sqlite_path=Path(_env("SQLITE_PATH", str(ROOT / "data" / "banque.db"))),
            llm_provider=llm_provider,
            llm_base_url=_env("LLM_BASE_URL", DEFAULT_LLM_URLS[llm_provider]).rstrip("/"),
            llm_model=_env("LLM_MODEL", "qwen2.5-coder:7b"),
            llm_api_key=_env("LLM_API_KEY", ""),
            llm_timeout_s=int(_env("LLM_TIMEOUT_S", "180")),
            llm_num_ctx=int(_env("LLM_NUM_CTX", "8192")),
            llm_keep_alive=_env("LLM_KEEP_ALIVE", "60m"),
            max_rows=int(_env("MAX_ROWS", "200")),
            query_timeout_s=int(_env("QUERY_TIMEOUT_S", "30")),
            max_retries=int(_env("MAX_RETRIES", "2")),
            summary_rows=int(_env("SUMMARY_ROWS", "30")),
            history_turns=int(_env("HISTORY_TURNS", "3")),
            semantic_file=Path(_env("SEMANTIC_FILE", str(ROOT / "semantic" / "banque.yaml"))),
            audit_log=Path(_env("AUDIT_LOG", str(ROOT / "logs" / "audit.jsonl"))),
            app_user=_env("APP_USER", "demo"),
        )
