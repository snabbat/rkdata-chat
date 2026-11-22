"""Garde-fou SQL : ce qui doit être refusé, ce qui doit passer, et le plafond de lignes."""

import pytest

from app.sql_guard import SQLGuardError, check_read_only, enforce_limit, extract_sql, guard


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE banque.clients",
        "SELECT * FROM banque.clients; DELETE FROM banque.clients",  # requêtes empilées
        "WITH c AS (SELECT 1) DELETE FROM banque.clients",
        "UPDATE banque.comptes SET solde = 0",
        "INSERT INTO banque.agences SELECT * FROM banque.agences",
        "CREATE TABLE banque.copie AS SELECT * FROM banque.clients",  # CTAS
        "REPLACE INTO banque.agences VALUES (1, 'x', 'y', 'z')",
        "SELECT * FROM banque.clients /* commentaire */ ; DROP TABLE banque.clients -- fin",
        "",
        "   ",
        "-- juste un commentaire",
    ],
)
def test_rejects_writes_ddl_and_empty(sql):
    with pytest.raises(SQLGuardError):
        guard(sql, 200)


def test_rejects_write_hidden_in_fenced_block():
    reply = "Voici la requête :\n```sql\nDELETE FROM banque.credits WHERE statut = 'Sain'\n```"
    with pytest.raises(SQLGuardError):
        guard(reply, 200)


def test_keywords_inside_string_literals_are_allowed():
    sql = "SELECT * FROM banque.transactions WHERE categorie = 'DROP TABLE; DELETE' LIMIT 5"
    assert check_read_only(sql) == sql


def test_keywords_inside_comments_are_ignored():
    sql = "SELECT 1 AS un -- puis on pourrait DROP la table\n"
    assert "DROP" not in check_read_only(sql)


def test_escaped_quote_does_not_end_the_string():
    sql = "SELECT 'l''agence; DELETE' AS libelle"
    assert check_read_only(sql) == sql


def test_replace_function_is_allowed():
    sql = "SELECT replace(ville, 'é', 'e') AS ville FROM banque.agences"
    assert check_read_only(sql) == sql


def test_column_names_containing_keywords_are_allowed():
    # `date_creation` ou `updated_at` ne sont pas les mots-clés CREATE / UPDATE.
    check_read_only("SELECT date_creation, updated_at FROM banque.t")


def test_extracts_sql_from_fenced_block():
    reply = "Bien sûr !\n```sql\nSELECT 1\n```\nCette requête renvoie 1."
    assert extract_sql(reply) == "SELECT 1"


def test_trailing_semicolon_is_tolerated():
    assert guard("```sql\nSELECT 1;\n```", 200) == "SELECT 1\nLIMIT 200"


def test_limit_is_appended_when_missing():
    assert enforce_limit("SELECT * FROM banque.clients ORDER BY nom", 200).endswith("ORDER BY nom\nLIMIT 200")


def test_limit_is_capped():
    assert enforce_limit("SELECT * FROM banque.clients LIMIT 5000", 200) == "SELECT * FROM banque.clients LIMIT 200"


def test_smaller_limit_is_kept():
    assert enforce_limit("SELECT * FROM banque.clients LIMIT 10", 200) == "SELECT * FROM banque.clients LIMIT 10"


def test_limit_in_subquery_does_not_count():
    sql = "SELECT * FROM banque.clients WHERE client_id IN (SELECT client_id FROM banque.credits LIMIT 5)"
    assert enforce_limit(sql, 200) == sql + "\nLIMIT 200"


def test_query_is_not_wrapped_in_a_subquery():
    # Envelopper dans SELECT * FROM (...) ferait perdre l'ORDER BY sous Trino.
    out = enforce_limit("SELECT nom FROM banque.clients ORDER BY nom DESC", 50)
    assert out.startswith("SELECT nom FROM banque.clients ORDER BY nom DESC")
    assert out.count("SELECT") == 1


def test_limit_survives_trailing_comment():
    # Un LIMIT ajouté sur la même ligne qu'un commentaire `--` serait neutralisé.
    out = guard("SELECT nom FROM banque.clients -- tous les clients", 200)
    assert out.splitlines()[-1] == "LIMIT 200"
    assert "--" not in out
