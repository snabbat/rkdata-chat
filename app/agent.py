"""Agent text-to-SQL : question en français -> SQL -> résultat -> synthèse -> graphique.

Déroulé d'une question :
  1. le LLM reçoit le prompt système (dialecte, schéma + descriptions, glossaire,
     règles), les exemples few-shot, les 3 derniers échanges, puis la question ;
  2. sa réponse passe par le garde-fou SQL (lecture seule, LIMIT) ;
  3. la requête est exécutée ; en cas d'erreur ou de refus, le message est renvoyé
     au LLM qui se corrige (2 nouvelles tentatives au plus) ;
  4. un second appel LLM rédige une courte synthèse à partir des 30 premières lignes ;
  5. un graphique est choisi automatiquement ;
  6. tout est tracé dans logs/audit.jsonl.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.config import Settings
from app.db import QueryError
from app.llm import LLMError
from app.semantic import few_shots, load_semantic, render_glossary, render_relations, render_schema
from app.sql_guard import SQLGuardError, enforce_limit, extract_sql, guard

logger = logging.getLogger(__name__)

OUT_OF_SCOPE = "HORS_PERIMETRE"
_OUT_OF_SCOPE_RE = re.compile(r"^[\s`*\"']*HORS[_ ]P[EÉ]RIM[EÈ]TRE\s*:?\s*(.*)", re.IGNORECASE | re.DOTALL)

OUT_OF_SCOPE_EXAMPLE = (
    "Quelle est la météo prévue demain ?",
    f"{OUT_OF_SCOPE}: la météo ne fait pas partie des données disponibles.",
)


# Messages qui ne sont pas des questions (« salut », « merci », « que sais-tu faire ? ») :
# sans cette porte de sortie, le modèle n'a le choix qu'entre du SQL et un refus, et
# recopie alors un exemple few-shot.
SMALL_TALK = "DISCUSSION"
_SMALL_TALK_RE = re.compile(r"^[\s`*\"']*DISCUSSION\s*:?\s*(.*)", re.IGNORECASE | re.DOTALL)
SMALL_TALK_EXAMPLE = (
    "Bonjour",
    f"{SMALL_TALK}: Bonjour ! Je réponds à vos questions sur les données disponibles : "
    "posez-moi une question métier, en français.",
)


NOT_UNDERSTOOD = (
    "Je n'ai pas reconnu de question sur les données dans ce message. Reformulez votre demande "
    "en précisant l'indicateur et l'axe d'analyse souhaités."
)


class _RecycledQuery(Exception):
    """Le modèle a recopié la requête d'une autre question au lieu de répondre à celle posée."""


def _squash(sql: str) -> str:
    """Forme canonique d'une requête, pour comparer deux textes SQL."""
    return " ".join(sql.split()).lower()


def _tokens(text: str) -> list[str]:
    """Mots en minuscules, sans accents ni ponctuation."""
    plain = unicodedata.normalize("NFKD", text.lower())
    plain = "".join(ch for ch in plain if not unicodedata.combining(ch))
    return re.findall(r"[a-z0-9]+", plain)


# --- Messages qui ne sont pas des questions -----------------------------------
# Reconnus par le code, sans appeler le modèle : un « merci » ne doit jamais
# déclencher une requête SQL, et un petit modèle n'est pas fiable sur ce tri.
_GREETINGS = {"salut", "bonjour", "bonsoir", "coucou", "hello", "hi", "hey", "salam", "bjr", "slt", "yo"}
_THANKS = {"merci", "remercie", "thanks", "thank", "thx"}
_GOODBYES = {"revoir", "bye", "bientot", "ciao", "journee", "soiree"}
_ACKS = {
    "ok", "okay", "oki", "oui", "non", "ouais", "super", "parfait", "top", "genial", "cool", "nickel",
    "bien", "entendu", "note", "compris", "accord", "excellent", "bravo", "impeccable", "magnifique",
}
_FILLERS = {
    "ca", "va", "comment", "vas", "tu", "allez", "vous", "et", "toi", "tres", "a", "tous", "toutes",
    "beaucoup", "bcp", "c", "est", "d", "je", "te", "pour", "ton", "ta", "votre", "aide", "la", "le",
    "reponse", "bonne", "au", "monsieur", "madame", "you", "l", "equipe", "tout",
}
_HELP_RE = re.compile(
    r"^(qui (es tu|etes vous)|(tu es|vous etes|c est) qui"
    r"|(que|qu est ce que) (peux tu|pouvez vous|sais tu|savez vous|tu peux|tu sais|vous pouvez|vous savez) faire"
    r"|tu (peux|sais) faire quoi|a quoi (sers tu|servez vous|ca sert)"
    r"|comment (ca|tu|vous) (marche|marches|fonctionne|fonctionnes|fonctionnez)"
    r"|comment (t|vous) utiliser|presente toi|presentez vous|que proposes tu"
    r"|aide|help|aide moi|aidez moi|besoin d aide|j ai besoin d aide)$"
)


def small_talk_reply(message: str, suggestions: list[str] | None = None) -> str | None:
    """Réponse toute faite si `message` est une formule de politesse ou une demande d'aide,
    None si c'est (peut-être) une question sur les données.

    Le message entier doit être fait de mots de politesse : « merci, et par segment ? »
    reste une question et part vers le modèle.
    """
    tokens = _tokens(message)
    examples = " ; ".join(f"« {q} »" for q in (suggestions or [])[:3])
    hint = f" Par exemple : {examples}." if examples else ""
    if not tokens:  # ponctuation ou émoji seul
        return "Posez-moi une question sur les données." + hint

    core = tokens[:]
    while core and core[0] in _GREETINGS:  # « bonjour, qui es-tu ? »
        core = core[1:]
    if core and _HELP_RE.match(" ".join(core)):
        return (
            "Je suis l'assistant données : je traduis vos questions en requêtes SQL, je les exécute "
            "en lecture seule, puis je vous rends une synthèse, un graphique et les données." + hint
        )

    words = set(tokens)
    if len(tokens) > 8 or not words <= (_GREETINGS | _THANKS | _GOODBYES | _ACKS | _FILLERS):
        return None
    if words & _THANKS:
        return "Avec plaisir ! Une autre question sur les données ?"
    if words & _GOODBYES:
        return "Au revoir, à bientôt !"
    if words & _GREETINGS or {"ca", "va"} <= words:
        return "Bonjour ! Je réponds à vos questions sur les données." + hint
    if words & _ACKS:
        return "Très bien. Posez-moi une autre question quand vous voulez."
    return None


# --- Pertinence d'une requête ---------------------------------------------------
_STOPWORDS = {
    "quel", "quelle", "quels", "quelles", "sont", "avec", "pour", "dans", "plus", "moins", "nous",
    "vous", "avons", "avez", "combien", "comment", "donne", "liste", "tous", "toutes", "leur",
    "leurs", "cette", "entre", "selon", "chaque", "etre", "avoir", "montre", "affiche",
}
_REPEAT_WORDS = {"meme", "memes", "precedent", "precedente", "encore", "pareil", "idem", "relance", "refais"}


def _related(question: str, reference: str) -> bool:
    """Deux questions partagent-elles au moins un mot porteur de sens ?"""
    words = set(_tokens(question))
    if words & _REPEAT_WORDS:  # « même chose », « relance la requête précédente »
        return True

    def content(tokens) -> set[str]:
        return {t for t in tokens if len(t) >= 4 and t not in _STOPWORDS}

    return bool(content(words) & content(_tokens(reference)))


DIALECT_HINTS = {
    "trino": (
        "Dialecte SQL : Trino.\n"
        "- Mois d'une date : date_trunc('month', colonne). Année : year(colonne).\n"
        "- Littéral de date : DATE '2026-01-31' (ne jamais comparer une date à une simple chaîne).\n"
        "- Filtre sur une année : colonne >= DATE '2026-01-01' AND colonne < DATE '2027-01-01'."
    ),
    "sqlite": (
        "Dialecte SQL : SQLite.\n"
        "- Les dates sont stockées en texte ISO 'AAAA-MM-JJ'.\n"
        "- Mois d'une date : strftime('%Y-%m', colonne). Année : strftime('%Y', colonne).\n"
        "- Filtre sur une année : colonne >= '2026-01-01' AND colonne < '2027-01-01'."
    ),
}

SYSTEM_PROMPT = """Tu es un analyste de données bancaires. Tu traduis des questions métier posées en français en UNE requête SQL de lecture.

## Contexte
{contexte}

## Dialecte
{dialecte}

## Schéma disponible
Toutes les tables doivent être préfixées par `{prefix}.` (exemple : {prefix}.{exemple_table}).

{schema}

## Jointures
{relations}

## Glossaire métier
{glossaire}

## Règles
1. Réponds uniquement par un bloc ```sql contenant une seule requête SELECT (ou WITH ... SELECT). Aucune explication, aucun texte autour.
2. Lecture seule : jamais d'INSERT, UPDATE, DELETE ni de DDL.
3. N'utilise que les tables et colonnes du schéma ci-dessus, toujours préfixées par `{prefix}.`. N'invente aucun nom.
4. Applique les définitions du glossaire et respecte l'orthographe exacte des valeurs (accents, majuscules).
5. Mets la dimension (date, mois, catégorie) en première colonne et les mesures ensuite, avec des alias explicites en français, sans accent ni espace.
6. Trie les séries temporelles par date croissante et les classements par mesure décroissante.
7. Si la question prolonge la précédente (« et pour... », « seulement... », « même chose... »), repars de la requête précédente et adapte-la.
8. Si la question ne peut pas être traitée avec ces données (hors sujet, information absente du schéma) ou demande de modifier des données, réponds exactement, sans SQL : {hors_perimetre}: <raison courte en français>
9. Si le message n'est pas une question sur les données (salutation, remerciement, question sur ton rôle ou sur ce que tu sais faire), n'écris aucun SQL et réponds : {discussion}: <réponse courte et polie en français, qui invite à poser une question sur les données>
"""

SUMMARY_PROMPT = """Tu es un analyste bancaire. On te donne une question métier, le résultat de la requête qui y répond et des repères déjà calculés.
Rédige en français une synthèse de 1 à 4 phrases qui répond directement à la question.
- Cite uniquement des valeurs qui figurent telles quelles dans le résultat ou dans les repères.
- Ne calcule rien toi-même : ni moyenne, ni somme, ni écart, ni pourcentage, ni variation.
- Pour une série temporelle, cite la première valeur, la dernière, le maximum et le minimum ; ne parle pas de tendance générale si la série fluctue.
- Les montants sont en MAD ; une colonne dont le nom finit par _pct est un pourcentage.
- Texte simple : pas de SQL, pas de tableau, pas de titre, pas de liste à puces."""

_TEMPORAL_VALUE_RE = re.compile(r"^\d{4}-\d{2}(-\d{2})?([ T].*)?$")
_TEMPORAL_NAME_RE = re.compile(
    r"(mois|month|date|jour|day|annee|année|year|trimestre|quarter|semaine|week|periode|période)", re.IGNORECASE
)


@dataclass
class Answer:
    question: str
    status: str = "ok"  # ok | discussion | hors_perimetre | refuse | erreur
    sql: str | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    truncated: bool = False  # True si le plafond MAX_ROWS a été atteint
    summary: str = ""
    chart: dict | None = None
    attempts: int = 0
    error: str | None = None
    model: str = ""
    timings: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def pick_chart(columns: list[str], rows: list[list]) -> dict | None:
    """Choisit un graphique : courbe si la 1re colonne est une date ou un mois,
    barres si c'est une catégorie — à condition d'avoir une mesure numérique et 2 à 60 lignes."""
    if len(columns) < 2 or not 2 <= len(rows) <= 60:
        return None

    def is_number(value) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    numeric = []
    for i in range(1, len(columns)):
        values = [row[i] for row in rows if row[i] is not None]
        if values and all(is_number(v) for v in values):
            numeric.append(columns[i])
    # Un identifiant n'est pas une mesure.
    measures = [c for c in numeric if not c.lower().endswith("_id")] or numeric
    if not measures:
        return None

    first = [row[0] for row in rows if row[0] is not None]
    if not first:
        return None
    if all(isinstance(v, str) and _TEMPORAL_VALUE_RE.match(v) for v in first):
        kind = "line"
    elif _TEMPORAL_NAME_RE.search(columns[0]) and all(is_number(v) or isinstance(v, str) for v in first):
        kind = "line"
    elif all(isinstance(v, str) for v in first):
        kind = "bar"
    else:
        return None
    # Par convention (règle 5 du prompt), la mesure principale est la dernière colonne.
    return {"type": kind, "x": columns[0], "y": measures[-1], "y_options": measures}


def _format_cell(value) -> str:
    """Format français (1 234 567,89) : le modèle recopie les valeurs telles quelles."""
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:,.2f}".replace(",", " ").replace(".", ",")
    return str(value)


def format_rows(columns: list[str], rows: list[list]) -> str:
    lines = [" | ".join(columns)]
    lines += [" | ".join(_format_cell(v) for v in row) for row in rows]
    return "\n".join(lines)


def key_figures(columns: list[str], rows: list[list], chart: dict | None) -> str:
    """Repères calculés en Python (maximum, minimum, première et dernière valeur).

    Un petit modèle se trompe dès qu'il doit calculer : on lui fournit donc des
    faits exacts à citer, plutôt que de le laisser inventer une moyenne.
    """
    if not chart or len(rows) < 2:
        return ""
    lines = []
    for measure in chart["y_options"]:
        i = columns.index(measure)
        points = [(row[i], row[0]) for row in rows if row[i] is not None]
        if len(points) < 2:
            continue
        (vmax, lmax), (vmin, lmin) = max(points, key=lambda p: p[0]), min(points, key=lambda p: p[0])
        facts = [f"maximum {_format_cell(vmax)} ({lmax})", f"minimum {_format_cell(vmin)} ({lmin})"]
        if chart["type"] == "line":
            (vfirst, lfirst), (vlast, llast) = points[0], points[-1]
            facts += [
                f"première valeur {_format_cell(vfirst)} ({lfirst})",
                f"dernière valeur {_format_cell(vlast)} ({llast})",
            ]
        lines.append(f"- {measure} : " + " ; ".join(facts))
    return "\n".join(lines)


class Agent:
    def __init__(self, db, llm, settings: Settings):
        self.db = db
        self.llm = llm
        self.settings = settings
        self.semantic = load_semantic(settings.semantic_file)
        self._db_schema: dict = {}
        self._schema_text: str | None = None
        self._audit_lock = threading.Lock()

    # --- Prompt ---------------------------------------------------------------
    def schema_text(self, refresh: bool = False) -> str:
        """Schéma introspecté fusionné avec les descriptions : ce que « voit » le LLM."""
        if self._schema_text is None or refresh:
            self._db_schema = self.db.schema()
            self._schema_text = render_schema(self._db_schema, self.semantic, self.db.prefix)
        return self._schema_text

    def system_prompt(self) -> str:
        schema = self.schema_text()
        return SYSTEM_PROMPT.format(
            contexte=" ".join(str(self.semantic.get("contexte", "")).split()),
            dialecte=DIALECT_HINTS[self.db.dialect],
            prefix=self.db.prefix,
            exemple_table=next(iter(self._db_schema), "table"),
            schema=schema,
            relations=render_relations(self.semantic),
            glossaire=render_glossary(self.semantic),
            hors_perimetre=OUT_OF_SCOPE,
            discussion=SMALL_TALK,
        )

    def build_messages(self, question: str, history: list[dict] | None) -> list[dict]:
        messages = [{"role": "system", "content": self.system_prompt()}]
        for q, sql in few_shots(self.semantic, self.db.prefix):
            messages.append({"role": "user", "content": q})
            messages.append({"role": "assistant", "content": f"```sql\n{sql}\n```"})
        # Exemple de refus, indépendant du domaine : montre au modèle le format attendu
        # plutôt que de le laisser inventer une table.
        messages.append({"role": "user", "content": OUT_OF_SCOPE_EXAMPLE[0]})
        messages.append({"role": "assistant", "content": OUT_OF_SCOPE_EXAMPLE[1]})
        messages.append({"role": "user", "content": SMALL_TALK_EXAMPLE[0]})
        messages.append({"role": "assistant", "content": SMALL_TALK_EXAMPLE[1]})
        # Mémoire courte : les derniers couples question/SQL, pour les relances.
        turns = [h for h in (history or []) if h.get("question") and h.get("sql")]
        for turn in turns[-self.settings.history_turns :]:
            messages.append({"role": "user", "content": turn["question"]})
            messages.append({"role": "assistant", "content": f"```sql\n{turn['sql']}\n```"})
        messages.append({"role": "user", "content": question})
        return messages

    # --- Question -> réponse --------------------------------------------------
    def ask(self, question: str, history: list[dict] | None = None, user: str | None = None) -> Answer:
        started = time.perf_counter()
        answer = Answer(question=question, model=getattr(self.llm, "model", ""))
        timings = {"llm_sql_s": 0.0, "sql_s": 0.0, "llm_synthese_s": 0.0}
        canned = small_talk_reply(question, self.semantic["questions_suggerees"])
        try:
            if canned:  # salutation, remerciement, demande d'aide : aucun appel au modèle
                answer.status, answer.summary = "discussion", canned
            else:
                self._answer(question, history, answer, timings)
        except LLMError as exc:
            answer.status, answer.error = "erreur", f"Modèle de langage indisponible : {exc}"
        except QueryError as exc:  # introspection du schéma impossible
            answer.status, answer.error = "erreur", f"Base de données indisponible : {exc}"
        timings["total_s"] = time.perf_counter() - started
        answer.timings = {k: round(v, 2) for k, v in timings.items()}
        self._audit(answer, user or self.settings.app_user)
        return answer

    def _answer(self, question: str, history: list[dict] | None, answer: Answer, timings: dict) -> None:
        messages = self.build_messages(question, history)
        # Requêtes que le modèle a sous les yeux (exemples few-shot et historique), avec leur
        # question : s'il en recopie une pour une question sans rapport, elle est refusée.
        known = [
            (q, _squash(enforce_limit(sql, self.settings.max_rows)))
            for q, sql in few_shots(self.semantic, self.db.prefix)
        ] + [(t["question"], _squash(t["sql"])) for t in (history or []) if t.get("question") and t.get("sql")]
        recycled = False
        result = None
        for attempt in range(1, self.settings.max_retries + 2):
            answer.attempts = attempt
            t0 = time.perf_counter()
            reply = self.llm.chat(messages)
            timings["llm_sql_s"] += time.perf_counter() - t0

            small_talk = _SMALL_TALK_RE.match(reply or "")
            if small_talk:
                # Le modèle signale un message qui n'est pas une question. On n'affiche pas son
                # texte libre (imprévisible : il peut se lancer dans une blague) mais une
                # réponse fixe, qui recentre sur les données.
                answer.status = "discussion"
                examples = " ; ".join(f"« {q} »" for q in self.semantic["questions_suggerees"][:3])
                answer.summary = "Je réponds uniquement aux questions sur les données." + (
                    f" Par exemple : {examples}." if examples else ""
                )
                answer.sql, answer.error = None, None
                return

            out_of_scope = _OUT_OF_SCOPE_RE.match(reply or "")
            if out_of_scope:
                answer.status = "hors_perimetre"
                reason = out_of_scope.group(1).strip().strip("`").strip() or "Question hors périmètre."
                # Après une recopie refusée, la « raison » du modèle commente parfois notre
                # remarque (« la requête précédente... ») au lieu de s'adresser à l'utilisateur.
                talks_about_query = recycled and re.search(r"requ[eê]te|sql", reason, re.IGNORECASE)
                answer.summary = NOT_UNDERSTOOD if talks_about_query else reason
                answer.sql, answer.error = None, None
                return

            try:
                answer.sql = guard(reply, self.settings.max_rows)
                # Garde-fou de pertinence : en difficulté, un petit modèle recopie un exemple ou
                # la requête d'une question précédente — elle s'exécuterait, mais répondrait à côté.
                squashed = _squash(answer.sql)
                if any(squashed == sql and not _related(question, q) for q, sql in known):
                    raise _RecycledQuery()
                t0 = time.perf_counter()
                try:
                    result = self.db.execute(answer.sql)
                finally:
                    timings["sql_s"] += time.perf_counter() - t0
                answer.status, answer.error = "ok", None
                break
            except SQLGuardError as exc:
                answer.status, answer.error = "refuse", f"Requête refusée par le garde-fou : {exc}"
                answer.sql = extract_sql(reply)[:2000] or None  # tracée dans l'audit, jamais exécutée
                feedback = (
                    f"Ta réponse a été refusée par le contrôle de sécurité : {exc}.\n"
                    "Réponds uniquement par un bloc ```sql contenant une seule requête SELECT de lecture seule."
                )
            except _RecycledQuery:
                recycled = True
                answer.status, answer.error = "hors_perimetre", None
                answer.sql, answer.summary = None, NOT_UNDERSTOOD
                feedback = (
                    "Cette requête répond à une autre question, pas au message de l'utilisateur.\n"
                    f"- Si ce message n'est pas une question sur les données, réponds : {SMALL_TALK}: <réponse courte>\n"
                    f"- Si l'information n'existe pas dans le schéma, réponds : {OUT_OF_SCOPE}: <raison courte>\n"
                    f"- Sinon, écris la requête SQL qui répond réellement à : « {question} »"
                )
            except QueryError as exc:
                answer.status, answer.error = "erreur", f"Erreur SQL : {exc}"
                feedback = (
                    f"La requête a échoué avec l'erreur suivante :\n{exc}\n"
                    "Corrige-la en n'utilisant que les tables et colonnes du schéma et réponds "
                    "uniquement par le bloc ```sql corrigé. Si l'information demandée n'existe pas "
                    f"dans le schéma, réponds à la place : {OUT_OF_SCOPE}: <raison courte>"
                )
            # Auto-correction : on renvoie l'erreur au modèle.
            messages = messages + [
                {"role": "assistant", "content": reply},
                {"role": "user", "content": feedback},
            ]

        if result is None:
            return
        answer.columns, answer.rows = result.columns, result.rows
        answer.truncated = len(result.rows) >= self.settings.max_rows
        answer.chart = pick_chart(result.columns, result.rows)
        if not result.rows:
            answer.summary = "Aucune ligne ne correspond à cette question."
            return
        t0 = time.perf_counter()
        try:
            answer.summary = self._summarize(question, result.columns, result.rows)
        except LLMError:
            answer.summary = "Synthèse indisponible (modèle injoignable) ; les données sont affichées ci-dessous."
        timings["llm_synthese_s"] = time.perf_counter() - t0

    def _summarize(self, question: str, columns: list[str], rows: list[list]) -> str:
        sample = rows[: self.settings.summary_rows]
        note = f"{len(rows)} ligne(s)"
        if len(rows) > len(sample):
            note += f", dont seules les {len(sample)} premières sont montrées"
        content = f"Question : {question}\n\nRésultat ({note}) :\n{format_rows(columns, sample)}"
        # Repères calculés sur les mêmes lignes que celles montrées au modèle.
        figures = key_figures(columns, sample, pick_chart(columns, sample))
        if figures:
            content += f"\n\nRepères calculés sur ces lignes (exacts, à citer tels quels) :\n{figures}"
        reply = self.llm.chat(
            [{"role": "system", "content": SUMMARY_PROMPT}, {"role": "user", "content": content}]
        )
        return (reply or "").strip()

    # --- Audit ----------------------------------------------------------------
    def _audit(self, answer: Answer, user: str) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "user": user,
            "question": answer.question,
            "status": answer.status,
            "sql": answer.sql,
            "rows": len(answer.rows),
            "attempts": answer.attempts,
            "error": answer.error,
            "model": answer.model,
            "backend": self.db.dialect,
            "timings": answer.timings,
        }
        path = Path(self.settings.audit_log)
        try:
            with self._audit_lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            # Visible dans les logs du conteneur ; la réponse reste servie à l'utilisateur.
            logger.exception("Écriture du journal d'audit impossible (%s)", path)
