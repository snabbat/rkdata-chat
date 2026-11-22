"""Garde-fou SQL : premier verrou « lecture seule », appliqué AVANT toute exécution.

Le LLM n'est jamais cru sur parole. Sa réponse doit contenir exactement une
requête SELECT (ou WITH ... SELECT), sans aucun mot-clé d'écriture ou de DDL,
et le nombre de lignes renvoyées est plafonné.

Ce verrou est doublé côté moteur (utilisateur Trino read-only, fichier SQLite
en mode=ro) : même s'il était contourné, l'écriture resterait impossible.
"""

from __future__ import annotations

import re


class SQLGuardError(ValueError):
    """Requête refusée ; le message est renvoyé au LLM pour qu'il se corrige."""


# Mots-clés d'écriture, de DDL et d'administration, interdits partout dans la requête.
FORBIDDEN_KEYWORDS = (
    "INSERT", "UPDATE", "DELETE", "MERGE", "UPSERT", "TRUNCATE",
    "DROP", "CREATE", "ALTER", "RENAME",
    "GRANT", "REVOKE", "DENY",
    "CALL", "EXEC", "EXECUTE",
    "COMMIT", "ROLLBACK",
    "ATTACH", "DETACH", "PRAGMA", "VACUUM", "REINDEX", "REFRESH",
)
_FORBIDDEN_RE = re.compile(r"\b(" + "|".join(FORBIDDEN_KEYWORDS) + r")\b", re.IGNORECASE)
# REPLACE est une instruction d'écriture (REPLACE INTO) mais aussi une fonction : replace(x, 'a', 'b').
_REPLACE_STATEMENT_RE = re.compile(r"\bREPLACE\b(?!\s*\()", re.IGNORECASE)
_STARTS_WITH_SELECT_RE = re.compile(r"^[\s(]*(SELECT|WITH)\b", re.IGNORECASE)

_FENCE_RE = re.compile(r"```[ \t]*(?:sql|trino|sqlite)?[ \t]*\r?\n?(.*?)```", re.IGNORECASE | re.DOTALL)
_FINAL_LIMIT_RE = re.compile(r"\bLIMIT\s+(\d+|ALL)(?:\s+OFFSET\s+\d+)?\s*$", re.IGNORECASE)
_FINAL_FETCH_RE = re.compile(r"\bFETCH\s+(?:FIRST|NEXT)\s+(\d+)\s+ROWS?\s+ONLY\s*$", re.IGNORECASE)


def extract_sql(text: str) -> str:
    """Extrait la requête d'une réponse de LLM (bloc ```sql ... ``` de préférence)."""
    match = _FENCE_RE.search(text or "")
    sql = match.group(1) if match else (text or "")
    return sql.strip()


def _scrub(sql: str) -> tuple[str, str]:
    """Renvoie (clean, masked), deux chaînes de même longueur que `sql`.

    - clean  : les commentaires sont remplacés par des espaces (chaînes intactes) ;
    - masked : en plus, le contenu des chaînes '...' et des identifiants "..."
               est remplacé par des espaces.

    L'analyse des mots-clés se fait sur `masked` : `WHERE libelle = 'DROP TABLE'`
    n'est donc pas pris pour une instruction DROP.
    """
    clean, masked = [], []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if ch == "-" and nxt == "-":  # commentaire jusqu'à la fin de ligne
            j = sql.find("\n", i)
            j = n if j == -1 else j
            clean.append(" " * (j - i))
            masked.append(" " * (j - i))
            i = j
        elif ch == "/" and nxt == "*":  # commentaire bloc
            j = sql.find("*/", i + 2)
            if j == -1:
                raise SQLGuardError("commentaire /* non fermé")
            clean.append(" " * (j + 2 - i))
            masked.append(" " * (j + 2 - i))
            i = j + 2
        elif ch in ("'", '"'):  # chaîne ou identifiant entre guillemets
            j = i + 1
            while True:
                j = sql.find(ch, j)
                if j == -1:
                    raise SQLGuardError("chaîne de caractères non fermée")
                if j + 1 < n and sql[j + 1] == ch:  # guillemet doublé = échappement
                    j += 2
                    continue
                break
            clean.append(sql[i : j + 1])
            masked.append(ch + " " * (j - i - 1) + ch)
            i = j + 1
        else:
            clean.append(ch)
            masked.append(ch)
            i += 1
    return "".join(clean), "".join(masked)


def _normalize(sql: str) -> tuple[str, str]:
    """Retire commentaires et point-virgule final ; renvoie (clean, masked) alignés."""
    clean, masked = _scrub(sql)
    while True:
        stripped = masked.rstrip()
        if stripped.endswith(";"):
            stripped = stripped[:-1]
            clean, masked = clean[: len(stripped)], stripped
            continue
        clean, masked = clean[: len(stripped)], stripped
        break
    lead = len(masked) - len(masked.lstrip())
    return clean[lead:], masked[lead:]


def check_read_only(sql: str) -> str:
    """Valide que `sql` est une unique requête de lecture. Renvoie la requête nettoyée."""
    clean, masked = _normalize(sql)
    if not masked.strip():
        raise SQLGuardError("aucune requête SQL trouvée dans la réponse")
    if ";" in masked:
        raise SQLGuardError("une seule requête est autorisée (pas de point-virgule intermédiaire)")
    if not _STARTS_WITH_SELECT_RE.match(masked):
        raise SQLGuardError("seules les requêtes commençant par SELECT ou WITH sont autorisées")
    forbidden = _FORBIDDEN_RE.search(masked) or _REPLACE_STATEMENT_RE.search(masked)
    if forbidden:
        raise SQLGuardError(
            f"mot-clé interdit « {forbidden.group(0).upper()} » : l'accès est en lecture seule"
        )
    return clean


def enforce_limit(sql: str, max_rows: int) -> str:
    """Plafonne le nombre de lignes en ajoutant ou en réduisant le LIMIT final.

    On n'enveloppe PAS la requête dans `SELECT * FROM (...) LIMIT n` : Trino ne
    garantit pas l'ORDER BY d'une sous-requête, le tri serait perdu.
    """
    clean, masked = _normalize(sql)
    match = _FINAL_LIMIT_RE.search(masked) or _FINAL_FETCH_RE.search(masked)
    if match:
        current = match.group(1)
        capped = max_rows if current.upper() == "ALL" else min(int(current), max_rows)
        return clean[: match.start(1)] + str(capped) + clean[match.end(1) :]
    return f"{clean}\nLIMIT {max_rows}"


def guard(llm_reply: str, max_rows: int) -> str:
    """Chaîne complète : extraction, contrôle lecture seule, plafonnement des lignes."""
    return enforce_limit(check_read_only(extract_sql(llm_reply)), max_rows)
