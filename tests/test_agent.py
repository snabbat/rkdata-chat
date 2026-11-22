"""Agent de bout en bout, hors-ligne : SQLite réel + LLM factice aux réponses scriptées."""

import json

import pytest

from app.agent import NOT_UNDERSTOOD, small_talk_reply
from app.db import QueryError
from app.semantic import few_shots
from tests.conftest import sql_block

TAUX_PAR_TYPE = """
SELECT type_credit,
       ROUND(100.0 * SUM(CASE WHEN statut IN ('Impayé', 'Contentieux') THEN encours ELSE 0 END)
             / NULLIF(SUM(encours), 0), 2) AS taux_impayes_pct
FROM banque.credits
GROUP BY type_credit
ORDER BY taux_impayes_pct DESC
"""

FLUX_MENSUEL = """
SELECT strftime('%Y-%m', date_operation) AS mois,
       SUM(CASE WHEN sens = 'CREDIT' THEN montant ELSE -montant END) AS flux_net
FROM banque.transactions
GROUP BY 1
ORDER BY 1
"""


def count(db, table: str) -> int:
    return db.execute(f"SELECT COUNT(*) FROM banque.{table}").rows[0][0]


# --- Verrou moteur ----------------------------------------------------------
def test_sqlite_file_is_read_only(db):
    """Le fichier est attaché en mode=ro : même sans garde-fou, l'écriture échoue."""
    before = count(db, "agences")
    for statement in (
        "INSERT INTO banque.agences VALUES (99, 'Pirate', 'Nulle part', 'Aucune')",
        "DELETE FROM banque.credits",
        "UPDATE banque.comptes SET solde = 0",
        "DROP TABLE banque.clients",
        "CREATE TABLE banque.intrus (x INTEGER)",
    ):
        with pytest.raises(QueryError):
            db.execute(statement)
    assert count(db, "agences") == before == 25


def test_schema_introspection(db):
    schema = db.schema()
    assert set(schema) == {"agences", "clients", "comptes", "transactions", "credits"}
    assert ("region", "varchar") in schema["agences"]


# --- Chemin nominal ---------------------------------------------------------
def test_happy_path_four_loan_types_and_bar_chart(make_agent, settings):
    agent, llm = make_agent([sql_block(TAUX_PAR_TYPE), "Le crédit à la consommation est le plus risqué."])
    answer = agent.ask("Quel est le taux d'impayés par type de crédit ?", user="alice")

    assert answer.status == "ok" and answer.error is None
    assert answer.attempts == 1
    assert len(answer.rows) == 4
    assert {row[0] for row in answer.rows} == {"Immobilier", "Consommation", "Auto", "Equipement"}
    assert answer.chart == {
        "type": "bar",
        "x": "type_credit",
        "y": "taux_impayes_pct",
        "y_options": ["taux_impayes_pct"],
    }
    assert answer.summary == "Le crédit à la consommation est le plus risqué."
    assert answer.sql.endswith("LIMIT 200")
    assert len(llm.calls) == 2  # 1 appel SQL + 1 appel synthèse

    # Le prompt système porte le schéma fusionné avec les descriptions et le glossaire.
    system = llm.calls[0][0]["content"]
    assert "Table banque.credits" in system
    assert "Capital restant dû à la date de référence" in system
    assert "taux d'impayés" in system
    assert "SQLite" in system

    # Journal d'audit
    record = json.loads(settings.audit_log.read_text(encoding="utf-8").splitlines()[-1])
    assert record["user"] == "alice"
    assert record["rows"] == 4 and record["attempts"] == 1 and record["error"] is None
    assert record["model"] == "fake-llm"
    assert "FROM banque.credits" in record["sql"]
    assert {"llm_sql_s", "sql_s", "llm_synthese_s", "total_s"} <= set(record["timings"])


def test_summary_only_sees_first_30_rows(make_agent):
    agent, llm = make_agent([sql_block("SELECT client_id, nom FROM banque.clients ORDER BY client_id"), "ok"])
    answer = agent.ask("Liste des clients")
    assert len(answer.rows) == 200 and answer.truncated  # plafond MAX_ROWS
    summary_input = llm.calls[1][-1]["content"]
    assert len(summary_input.split("Résultat")[1].splitlines()) == 1 + 1 + 30  # titre + en-tête + 30 lignes


def test_summary_receives_exact_key_figures(make_agent, db):
    """Le modèle ne calcule rien : maximum, minimum, première et dernière valeur lui sont fournis."""
    agent, llm = make_agent([sql_block(FLUX_MENSUEL), "ok"])
    agent.ask("Évolution mensuelle du flux net")
    summary_input = llm.calls[1][-1]["content"]

    rows = db.execute(FLUX_MENSUEL).rows
    best = max(rows, key=lambda r: r[1])
    expected_max = f"{best[1]:,.2f}".replace(",", " ").replace(".", ",")
    assert "Repères calculés" in summary_input
    assert f"maximum {expected_max} ({best[0]})" in summary_input
    assert "première valeur" in summary_input and "(2025-01)" in summary_input
    assert "dernière valeur" in summary_input and "(2026-10)" in summary_input


# --- Auto-correction --------------------------------------------------------
def test_self_correction_after_unknown_column(make_agent):
    agent, llm = make_agent(
        [
            sql_block("SELECT type_pret, COUNT(*) AS nb FROM banque.credits GROUP BY type_pret"),
            sql_block("SELECT type_credit, COUNT(*) AS nb FROM banque.credits GROUP BY type_credit"),
            "Quatre types de crédit.",
        ]
    )
    answer = agent.ask("Combien de crédits par type ?")

    assert answer.status == "ok" and answer.error is None
    assert answer.attempts == 2
    assert len(answer.rows) == 4
    # La 2e tentative a bien reçu la requête fautive et l'erreur du moteur.
    retry = llm.calls[1]
    assert "type_pret" in retry[-2]["content"]
    assert retry[-1]["role"] == "user" and "type_pret" in retry[-1]["content"]


def test_gives_up_after_two_retries(make_agent):
    bad = sql_block("SELECT inconnue FROM banque.credits")
    agent, llm = make_agent([bad, bad, bad])
    answer = agent.ask("Question impossible")
    assert answer.status == "erreur"
    assert answer.attempts == 3 and len(llm.calls) == 3
    assert "inconnue" in answer.error
    assert answer.rows == []


# --- Écritures --------------------------------------------------------------
def test_write_queries_are_blocked_and_data_unchanged(make_agent, db, settings):
    before = count(db, "credits")
    agent, llm = make_agent(
        [
            sql_block("DELETE FROM banque.credits WHERE statut = 'Contentieux'"),
            sql_block("SELECT 1; DROP TABLE banque.credits"),
            sql_block("UPDATE banque.credits SET encours = 0"),
        ]
    )
    answer = agent.ask("Supprime les crédits en contentieux")

    assert answer.status == "refuse"
    assert answer.attempts == 3
    assert "garde-fou" in answer.error
    assert answer.rows == [] and answer.chart is None
    assert count(db, "credits") == before == 800
    # Le refus est expliqué au modèle, et tracé dans l'audit.
    assert "refusée" in llm.calls[1][-1]["content"]
    record = json.loads(settings.audit_log.read_text(encoding="utf-8").splitlines()[-1])
    assert record["status"] == "refuse" and record["rows"] == 0


# --- Hors périmètre ---------------------------------------------------------
def test_out_of_scope_question(make_agent):
    agent, llm = make_agent(["HORS_PERIMETRE: la météo ne figure pas dans les données bancaires."])
    answer = agent.ask("Quel temps fera-t-il demain à Casablanca ?")

    assert answer.status == "hors_perimetre"
    assert answer.summary == "la météo ne figure pas dans les données bancaires."
    assert answer.sql is None and answer.rows == [] and answer.error is None
    assert len(llm.calls) == 1  # ni exécution SQL, ni synthèse


# --- Relances ---------------------------------------------------------------
def test_history_is_sent_with_follow_up_questions(make_agent):
    agent, llm = make_agent([sql_block(FLUX_MENSUEL), "Synthèse."])
    history = [
        {"question": f"Question {i}", "sql": f"SELECT {i} AS n FROM banque.agences"} for i in range(1, 6)
    ]
    agent.ask("et seulement pour Casablanca-Settat ?", history=history)

    messages = llm.calls[0]
    contents = [m["content"] for m in messages]
    # Seuls les 3 derniers échanges sont rejoués, dans l'ordre, avant la nouvelle question.
    assert "Question 2" not in contents
    assert contents[-7:-1:2] == ["Question 3", "Question 4", "Question 5"]
    assert "SELECT 5 AS n" in contents[-2] and messages[-2]["role"] == "assistant"
    assert messages[-1] == {"role": "user", "content": "et seulement pour Casablanca-Settat ?"}
    # system + 2 exemples few-shot + 1 refus + 1 salutation + 3 échanges (x2 messages chacun) + question
    assert len(messages) == 1 + 2 * 2 + 2 + 2 + 3 * 2 + 1
    # Les exemples few-shot portent le préfixe du moteur, plus le placeholder.
    assert "FROM banque.credits" in contents[2] and "{schema}" not in "".join(contents)
    assert contents[6].startswith("HORS_PERIMETRE:")
    assert contents[7] == "Bonjour" and contents[8].startswith("DISCUSSION:")


# --- Messages qui ne sont pas des questions -----------------------------------
@pytest.mark.parametrize(
    "message",
    [
        "salut",
        "Bonjour !",
        "Bonjour, ça va ?",
        "merci beaucoup",
        "ok super",
        "Parfait, merci !",
        "au revoir",
        "Qui es-tu ?",
        "Bonjour, que peux-tu faire ?",
        "aide",
        "👍",
    ],
)
def test_small_talk_never_reaches_the_model(make_agent, settings, message):
    """« salut » n'est pas une question : réponse toute faite, sans LLM, sans SQL, sans graphique."""
    agent, llm = make_agent([])  # aucune réponse scriptée : tout appel au modèle ferait échouer le test
    answer = agent.ask(message)

    assert answer.status == "discussion" and answer.summary
    assert answer.sql is None and answer.rows == [] and answer.chart is None and answer.error is None
    assert llm.calls == [] and answer.attempts == 0
    record = json.loads(settings.audit_log.read_text(encoding="utf-8").splitlines()[-1])
    assert record["status"] == "discussion" and record["sql"] is None


@pytest.mark.parametrize(
    "message",
    [
        "merci, et par segment ?",
        "ok et pour Casablanca-Settat ?",
        "Bonjour, quel est le taux d'impayés par région ?",
        "et en 2025 ?",
        "Combien de clients ?",
        "Top 10",
    ],
)
def test_questions_are_not_mistaken_for_small_talk(message):
    assert small_talk_reply(message) is None


def test_help_reply_suggests_questions_from_the_semantic_layer(make_agent):
    agent, _ = make_agent([])
    answer = agent.ask("Que peux-tu faire ?")
    assert "lecture seule" in answer.summary
    assert agent.semantic["questions_suggerees"][0] in answer.summary


def test_model_can_also_decline_to_query(make_agent):
    """Second filet : pour un message que le code ne reconnaît pas, le modèle peut répondre
    DISCUSSION au lieu de produire du SQL."""
    agent, llm = make_agent(["DISCUSSION: Je ne raconte pas de blagues, mais je connais vos données."])
    answer = agent.ask("Raconte-moi une blague")

    assert answer.status == "discussion"
    # Le texte libre du modèle n'est pas affiché : réponse fixe, recentrée sur les données.
    assert answer.summary.startswith("Je réponds uniquement aux questions sur les données.")
    assert "blague" not in answer.summary
    assert answer.sql is None and answer.rows == [] and len(llm.calls) == 1
    assert "DISCUSSION" in llm.calls[0][0]["content"]  # la règle figure dans le prompt système


def test_recycled_previous_query_is_not_accepted_as_a_fix(make_agent):
    """En difficulté, le modèle recopie parfois la requête précédente : elle s'exécuterait
    sans erreur mais répondrait à une autre question. L'agent la refuse."""
    previous = "SELECT segment, COUNT(*) AS nb_clients FROM banque.clients GROUP BY segment\nLIMIT 200"
    agent, llm = make_agent(
        [
            sql_block("SELECT AVG(salaire) AS salaire_moyen FROM banque.employes"),  # table inventée
            sql_block(previous),  # recopie de l'historique
            "HORS_PERIMETRE: les données RH ne figurent pas dans le schéma.",
        ]
    )
    answer = agent.ask(
        "Quel est le salaire moyen des employés ?",
        history=[{"question": "Répartition des clients par segment", "sql": previous}],
    )
    assert answer.status == "hors_perimetre" and answer.attempts == 3
    assert answer.rows == [] and answer.sql is None
    assert answer.summary == "les données RH ne figurent pas dans le schéma."
    assert "autre question" in llm.calls[2][-1]["content"]
    assert "HORS_PERIMETRE" in llm.calls[1][-1]["content"]  # la porte de sortie est rappelée


def test_copied_example_query_is_not_served_for_an_unrelated_message(make_agent):
    """Cas réel : à un message sans rapport, le modèle 7B répondait par le SQL d'un exemple
    few-shot — des indicateurs que personne n'avait demandés."""
    agent, _ = make_agent([])
    copied = sql_block(few_shots(agent.semantic, "banque")[1][1])
    agent, llm = make_agent([copied, copied, copied])
    answer = agent.ask("Raconte-moi une blague")

    assert answer.status == "hors_perimetre" and answer.summary == NOT_UNDERSTOOD
    assert answer.attempts == 3 and answer.rows == [] and answer.sql is None and answer.chart is None
    assert "DISCUSSION" in llm.calls[1][-1]["content"]  # la porte de sortie est rappelée


def test_example_query_is_accepted_for_a_matching_question(make_agent):
    """La même requête reste légitime quand la question est bien celle de l'exemple, reformulée."""
    agent, _ = make_agent([])
    copied = sql_block(few_shots(agent.semantic, "banque")[1][1])
    agent, _ = make_agent([copied, "Synthèse."])
    answer = agent.ask("Top 5 des agences par nombre de clients actifs")
    assert answer.status == "ok" and len(answer.rows) == 5


def test_same_question_asked_twice_is_still_answered(make_agent):
    """La vérification ci-dessus ne s'applique qu'aux corrections, pas à une question répétée."""
    previous = "SELECT segment, COUNT(*) AS nb_clients FROM banque.clients GROUP BY segment\nLIMIT 200"
    agent, _ = make_agent([sql_block(previous), "Trois segments."])
    answer = agent.ask(
        "Répartition des clients par segment",
        history=[{"question": "Répartition des clients par segment", "sql": previous}],
    )
    assert answer.status == "ok" and len(answer.rows) == 3


# --- Série temporelle -------------------------------------------------------
def test_monthly_series_has_22_points_and_line_chart(make_agent):
    agent, _ = make_agent([sql_block(FLUX_MENSUEL), "Le flux net reste positif."])
    answer = agent.ask("Évolution mensuelle du flux net")

    assert answer.status == "ok"
    assert len(answer.rows) == 22  # janvier 2025 -> octobre 2026
    assert answer.rows[0][0] == "2025-01" and answer.rows[-1][0] == "2026-10"
    assert answer.chart["type"] == "line"
    assert answer.chart["x"] == "mois" and answer.chart["y"] == "flux_net"
