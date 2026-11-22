"""Accès aux données : Trino (lakehouse Iceberg) ou SQLite (mode hors-ligne).

Les deux moteurs exposent la même interface :
  - execute(sql)  -> QueryResult(columns, rows)
  - schema()      -> {table: [(colonne, type), ...]}  (introspection)
  - health()      -> (ok, message)
  - prefix        -> préfixe obligatoire des tables dans le SQL généré

Chaque moteur porte son propre verrou « lecture seule », indépendant du garde-fou
SQL de l'application :
  - Trino  : l'utilisateur `chatbot` n'a que des droits read-only (rules.json) ;
  - SQLite : le fichier est attaché en mode=ro.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime
from decimal import Decimal
from pathlib import Path

from app.config import Settings


class QueryError(Exception):
    """Erreur d'exécution SQL ; le message est renvoyé au LLM pour auto-correction."""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list]


def _to_plain(value):
    """Convertit les types SQL en types simples (JSON, pandas, graphiques)."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date, dtime)):
        return value.isoformat()
    return value


class TrinoDatabase:
    dialect = "trino"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.prefix = f"{settings.trino_catalog}.{settings.trino_schema}"
        self.label = f"Trino · {self.prefix}"

    def _connect(self):
        import trino  # import tardif : inutile en mode SQLite et pour les tests

        s = self.settings
        return trino.dbapi.connect(
            host=s.trino_host,
            port=s.trino_port,
            user=s.trino_user,
            catalog=s.trino_catalog,
            schema=s.trino_schema,
            http_scheme=s.trino_http_scheme,
            source="rkdata-chat",
            # Limite côté moteur : Trino interrompt lui-même les requêtes trop longues.
            session_properties={"query_max_execution_time": f"{s.query_timeout_s}s"},
        )

    def execute(self, sql: str) -> QueryResult:
        try:
            conn = self._connect()
            try:
                cursor = conn.cursor()
                cursor.execute(sql)
                rows = cursor.fetchall()
                columns = [d[0] for d in cursor.description or []]
            finally:
                conn.close()
        except Exception as exc:  # TrinoUserError, erreurs réseau...
            raise QueryError(getattr(exc, "message", None) or str(exc)) from exc
        return QueryResult(columns, [[_to_plain(v) for v in row] for row in rows])

    def schema(self) -> dict[str, list[tuple[str, str]]]:
        s = self.settings
        result = self.execute(
            "SELECT table_name, column_name, data_type "
            f"FROM {s.trino_catalog}.information_schema.columns "
            f"WHERE table_schema = '{s.trino_schema}' "
            "ORDER BY table_name, ordinal_position"
        )
        tables: dict[str, list[tuple[str, str]]] = {}
        for table, column, data_type in result.rows:
            tables.setdefault(table, []).append((column, data_type))
        return tables

    def health(self) -> tuple[bool, str]:
        try:
            self.execute("SELECT 1")
            return True, "connecté"
        except QueryError as exc:
            return False, str(exc)[:200]


class SQLiteDatabase:
    dialect = "sqlite"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.path = Path(settings.sqlite_path)
        # Le fichier est attaché sous le nom du schéma : `banque.table` fonctionne
        # donc à l'identique en SQLite et (avec le catalogue devant) en Trino.
        self.prefix = settings.trino_schema
        self.label = f"SQLite · {self.path.name} (hors-ligne)"

    def _connect(self) -> sqlite3.Connection:
        if not self.path.exists():
            raise QueryError(
                f"Base SQLite introuvable : {self.path}. Lancez `python scripts/generate_data.py`."
            )
        # Base principale en mémoire (vide) + fichier attaché en LECTURE SEULE.
        conn = sqlite3.connect(":memory:", uri=True)
        conn.execute(f"ATTACH DATABASE ? AS {self.prefix}", (self.path.resolve().as_uri() + "?mode=ro",))
        conn.execute("PRAGMA query_only = ON")
        return conn

    def execute(self, sql: str) -> QueryResult:
        conn = self._connect()
        # Délai maximal : SQLite appelle ce handler toutes les 10 000 instructions
        # de sa machine virtuelle ; renvoyer 1 interrompt la requête.
        deadline = time.monotonic() + self.settings.query_timeout_s
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
        try:
            cursor = conn.execute(sql)
            rows = cursor.fetchall()
            columns = [d[0] for d in cursor.description or []]
        except sqlite3.Error as exc:
            if "interrupted" in str(exc):
                raise QueryError(
                    f"Requête interrompue : délai de {self.settings.query_timeout_s} s dépassé."
                ) from exc
            raise QueryError(str(exc)) from exc
        finally:
            conn.close()
        return QueryResult(columns, [[_to_plain(v) for v in row] for row in rows])

    def schema(self) -> dict[str, list[tuple[str, str]]]:
        conn = self._connect()
        try:
            names = [
                r[0]
                for r in conn.execute(
                    f"SELECT name FROM {self.prefix}.sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            return {
                name: [
                    (col[1], col[2].lower())
                    for col in conn.execute(f'PRAGMA {self.prefix}.table_info("{name}")')
                ]
                for name in names
            }
        finally:
            conn.close()

    def health(self) -> tuple[bool, str]:
        try:
            self.execute("SELECT 1")
            return True, "connecté"
        except QueryError as exc:
            return False, str(exc)[:200]


def get_database(settings: Settings):
    return TrinoDatabase(settings) if settings.db_backend == "trino" else SQLiteDatabase(settings)
