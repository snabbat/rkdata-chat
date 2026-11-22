"""Couche sémantique : lecture du YAML et fusion avec le schéma réel de la base.

Le schéma physique (tables, colonnes, types) vient de l'introspection du moteur ;
le sens métier (descriptions, glossaire, exemples) vient de semantic/banque.yaml.
Le LLM voit la fusion des deux.
"""

from __future__ import annotations

from pathlib import Path

import yaml


def load_semantic(path: Path) -> dict:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    data.setdefault("tables", {})
    data.setdefault("relations", [])
    data.setdefault("glossaire", [])
    data.setdefault("exemples", [])
    data.setdefault("questions_suggerees", [])
    return data


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def render_schema(db_schema: dict[str, list[tuple[str, str]]], semantic: dict, prefix: str) -> str:
    """Schéma introspecté + descriptions du YAML, au format texte lu par le LLM."""
    blocks = []
    for table, columns in db_schema.items():
        spec = semantic["tables"].get(table, {})
        descriptions = spec.get("colonnes", {})
        lines = [f"Table {prefix}.{table}" + (f" — {_one_line(spec['description'])}" if spec.get("description") else "")]
        for column, data_type in columns:
            description = descriptions.get(column)
            lines.append(f"  - {column} ({data_type})" + (f" : {_one_line(description)}" if description else ""))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def render_relations(semantic: dict) -> str:
    return "\n".join(f"- {_one_line(r)}" for r in semantic["relations"])


def render_glossary(semantic: dict) -> str:
    return "\n".join(f"- {g['terme']} : {_one_line(g['definition'])}" for g in semantic["glossaire"])


def few_shots(semantic: dict, prefix: str) -> list[tuple[str, str]]:
    """Exemples (question, SQL) avec le placeholder {schema} remplacé par le préfixe du moteur."""
    return [(e["question"], e["sql"].strip().replace("{schema}", prefix)) for e in semantic["exemples"]]
