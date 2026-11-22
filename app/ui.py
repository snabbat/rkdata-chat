"""Interface Streamlit — « Chat avec votre Lakehouse », aux couleurs de RK Data.

Lancement : streamlit run app/ui.py
"""

from __future__ import annotations

import base64
import html
import re
import sys
import threading
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.agent import Agent  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import get_database  # noqa: E402
from app.llm import get_llm  # noqa: E402

# --- Charte RK Data v1.0 ------------------------------------------------------
BRAND = "#1654F2"  # bleu de marque
NIGHT = "#171A27"  # bleu nuit (en-tête)
SKY = "#32AAFB"  # bleu ciel (petits accents)
SLATE = "#5B6178"  # ardoise (texte secondaire)
MIST = "#F3F6FD"  # brume (barre latérale)
GRID = "#E3E8F4"
OK_GREEN = "#0B7A53"
KO_RED = "#B42318"
# Source Sans est embarquée par Streamlit : aucun appel à un CDN de polices.
FONT = '"Source Sans", "Source Sans 3", "Source Sans Pro", sans-serif'
ASSETS = Path(__file__).resolve().parent / "assets"

CSS = f"""
<style>
[data-testid="stSidebar"] {{ background: {MIST}; border-right: 1px solid {GRID}; }}
.block-container {{ padding-top: 2.2rem; max-width: 1100px; }}
.rk-header {{
  background: {NIGHT}; color: #FFFFFF; border-radius: 12px; padding: 20px 26px;
  border-bottom: 3px solid {SKY}; display: flex; align-items: center; gap: 26px;
  margin-bottom: 1.2rem; font-family: {FONT};
}}
.rk-header img {{ height: 30px; flex: none; }}
.rk-header .rk-title {{ font-size: 1.45rem; font-weight: 700; line-height: 1.2; }}
.rk-header .rk-sub {{ font-size: 0.92rem; color: #C5CCE0; margin-top: 3px; }}
.rk-side-logo {{ height: 26px; margin: 2px 0 14px 2px; }}
.rk-side-title {{
  font-family: {FONT}; font-size: 0.74rem; font-weight: 700; letter-spacing: .08em;
  text-transform: uppercase; color: {SLATE}; margin: 1.1rem 0 .35rem 0;
}}
.rk-status {{
  display: flex; justify-content: space-between; gap: 10px; font-family: {FONT};
  font-size: 0.86rem; padding: 3px 0; border-bottom: 1px solid {GRID};
}}
.rk-status .rk-k {{ color: {SLATE}; flex: none; }}
.rk-status .rk-v {{ color: {NIGHT}; font-weight: 600; text-align: right; overflow-wrap: anywhere; }}
.rk-status .rk-ok {{ color: {OK_GREEN}; }}
.rk-status .rk-ko {{ color: {KO_RED}; }}
.rk-empty {{
  border: 1px dashed #C9D3EC; border-radius: 12px; padding: 22px 26px; color: {SLATE};
  font-family: {FONT}; background: #FBFCFF;
}}
.rk-empty b {{ color: {NIGHT}; }}
</style>
"""


def svg_data_uri(name: str) -> str:
    data = (ASSETS / name).read_bytes()
    return "data:image/svg+xml;base64," + base64.b64encode(data).decode("ascii")


# --- Ressources partagées -----------------------------------------------------
@st.cache_resource(show_spinner=False)
def get_agent() -> Agent:
    settings = Settings.from_env()
    agent = Agent(get_database(settings), get_llm(settings), settings)
    # Précharge le modèle sans bloquer l'affichage de la page.
    threading.Thread(target=agent.llm.warmup, daemon=True).start()
    return agent


@st.cache_data(ttl=20, show_spinner=False)
def get_health() -> dict:
    agent = get_agent()
    return {"db": agent.db.health(), "llm": agent.llm.health()}


def current_user(settings: Settings) -> str:
    """Identité tracée dans l'audit : en-tête posé par un reverse-proxy SSO, sinon APP_USER."""
    try:
        return st.context.headers.get("X-Forwarded-User") or settings.app_user
    except Exception:
        return settings.app_user


# --- Mise en forme ------------------------------------------------------------
def humanize(column: str) -> str:
    return column.replace("_", " ").strip().capitalize()


def fr_number(value) -> str:
    """Nombre au format français : 1 234 567 ou 12,35."""
    if value is None or pd.isna(value):
        return ""
    decimals = 0 if abs(value) >= 1000 or float(value).is_integer() else 2
    return f"{value:,.{decimals}f}".replace(",", " ").replace(".", ",")


def fr_seconds(value) -> str:
    return f"{value:.1f}".replace(".", ",") + " s"


# --- Graphique ----------------------------------------------------------------
def build_chart(df: pd.DataFrame, kind: str, x: str, y: str) -> alt.TopLevelMixin:
    """Une seule série, une seule couleur de marque : le titre nomme la mesure."""
    labels = df[x].astype(str)
    if labels.str.fullmatch(r"\d{4}-\d{2}-01").all():  # date_trunc('month') -> 2026-03
        labels = labels.str.slice(0, 7)
    data = pd.DataFrame({"x": labels, "y": pd.to_numeric(df[y]), "y_fr": df[y].map(fr_number)})
    data = data.dropna(subset=["y"])
    big = data["y"].abs().max() >= 1000
    # Graduations au format français (espace pour les milliers, virgule décimale).
    label_expr = (
        "replace(format(datum.value, ',.0f'), /,/g, ' ')"
        if big
        else "replace(format(datum.value, ',.2~f'), '.', ',')"
    )
    measure_axis = alt.Axis(labelExpr=label_expr, tickCount=5, grid=True, domain=False, ticks=False)
    tooltip = [alt.Tooltip("x:N", title=humanize(x)), alt.Tooltip("y_fr:N", title=humanize(y))]
    title = alt.TitleParams(
        f"{humanize(y)} par {humanize(x).lower()}", anchor="start", color=NIGHT, fontSize=14, fontWeight=600
    )

    if kind == "line":
        base = alt.Chart(data).encode(
            x=alt.X("x:N", sort=None, title=None, axis=alt.Axis(labelAngle=0 if len(data) <= 12 else -45, grid=False)),
            y=alt.Y("y:Q", title=None, axis=measure_axis),
        )
        layers = [
            base.mark_line(color=BRAND, strokeWidth=2),
            base.mark_point(color=BRAND, filled=True, opacity=1, size=64, stroke="#FFFFFF", strokeWidth=1.5),
            # Cible de survol plus large que le point lui-même.
            base.mark_point(size=500, opacity=0).encode(tooltip=tooltip),
        ]
        if data["y"].min() < 0 < data["y"].max():  # repère du zéro quand la série change de signe
            zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(color=SLATE, strokeWidth=1).encode(y="y:Q")
            layers.insert(0, zero)
        chart = alt.layer(*layers).properties(height=320, title=title)
    else:
        # Barres horizontales : les libellés de catégories restent lisibles.
        chart = (
            alt.Chart(data)
            .mark_bar(color=BRAND, cornerRadiusEnd=4, size=16 if len(data) <= 20 else 12)
            .encode(
                y=alt.Y(
                    "x:N",
                    sort=None,
                    title=None,
                    axis=alt.Axis(labelLimit=260, grid=False, ticks=False, domain=False, labelPadding=8),
                    scale=alt.Scale(paddingInner=0.35, paddingOuter=0.2),
                ),
                x=alt.X("y:Q", title=None, axis=measure_axis),
                tooltip=tooltip,
            )
            .properties(height=alt.Step(30 if len(data) <= 20 else 20), title=title)
        )
    return (
        chart.configure(font=FONT, background="transparent")
        .configure_view(stroke=None)
        .configure_axis(labelColor=SLATE, labelFontSize=12, gridColor=GRID, domainColor="#C9D1E6", tickColor="#C9D1E6")
    )


# --- Rendu d'une réponse --------------------------------------------------------
def render_answer(answer: dict, index: int) -> None:
    status = answer["status"]
    timings = answer.get("timings", {})

    if status == "discussion":  # salutation, remerciement... : simple texte, sans SQL
        st.markdown(answer["summary"].replace("$", "\\$"))
        return

    if status == "hors_perimetre":
        st.info(f"**Question hors périmètre.** {answer['summary']}", icon=":material/block:")
        return

    if status in ("refuse", "erreur"):
        st.error(answer.get("error") or "Une erreur est survenue.", icon=":material/error:")
        if answer.get("sql"):
            label = "Requête refusée (non exécutée)" if status == "refuse" else "Dernière requête tentée"
            with st.expander(f"{label} · {answer['attempts']} tentative(s)"):
                st.code(answer["sql"], language="sql")
        return

    st.markdown(answer["summary"].replace("$", "\\$"))
    df = pd.DataFrame(answer["rows"], columns=answer["columns"])

    chart = answer.get("chart")
    if chart:
        measure = chart["y"]
        if len(chart["y_options"]) > 1:
            measure = st.selectbox(
                "Mesure affichée",
                chart["y_options"],
                index=chart["y_options"].index(chart["y"]),
                format_func=humanize,
                key=f"measure_{index}",
            )
        st.altair_chart(build_chart(df, chart["type"], chart["x"], measure), theme=None, width="stretch")

    count = len(df)
    capped = " (plafond atteint)" if answer.get("truncated") else ""
    with st.expander(f"Données · {count} ligne{'s' if count > 1 else ''}{capped}"):
        st.dataframe(df, hide_index=True, width="stretch")
        st.download_button(
            "Exporter en CSV",
            # Séparateur ; et BOM UTF-8 : s'ouvre directement dans Excel en français.
            data=df.to_csv(index=False, sep=";", decimal=",").encode("utf-8-sig"),
            file_name=f"resultat_{index}.csv",
            mime="text/csv",
            icon=":material/download:",
            key=f"csv_{index}",
        )

    attempts = answer["attempts"]
    with st.expander(
        f"Requête SQL · {attempts} tentative{'s' if attempts > 1 else ''} · {fr_seconds(timings.get('total_s', 0))}"
    ):
        st.code(answer["sql"], language="sql")
        st.caption(
            f"Génération SQL : {fr_seconds(timings.get('llm_sql_s', 0))} · "
            f"Exécution : {fr_seconds(timings.get('sql_s', 0))} · "
            f"Synthèse : {fr_seconds(timings.get('llm_synthese_s', 0))} · "
            f"Modèle : {answer.get('model', '')}"
        )


# --- Barre latérale -------------------------------------------------------------
def status_row(label: str, value: str, css: str = "") -> str:
    return (
        f'<div class="rk-status"><span class="rk-k">{html.escape(label)}</span>'
        f'<span class="rk-v {css}">{html.escape(value)}</span></div>'
    )


def render_sidebar(agent: Agent) -> str | None:
    """Affiche la barre latérale ; renvoie la question d'exemple cliquée, le cas échéant."""
    settings = agent.settings
    clicked = None
    with st.sidebar:
        st.markdown(
            f'<img class="rk-side-logo" src="{svg_data_uri("logo-rkdata.svg")}" alt="RK Data">',
            unsafe_allow_html=True,
        )
        if st.button("Nouvelle conversation", icon=":material/add_comment:", type="primary", width="stretch"):
            st.session_state.messages = []
            st.rerun()

        st.markdown('<div class="rk-side-title">Exemples de questions</div>', unsafe_allow_html=True)
        for i, question in enumerate(agent.semantic["questions_suggerees"][:6]):
            if st.button(question, key=f"example_{i}", width="stretch"):
                clicked = question

        st.markdown('<div class="rk-side-title">État du système</div>', unsafe_allow_html=True)
        health = get_health()
        db_ok, db_msg = health["db"]
        llm_ok, llm_msg = health["llm"]
        rows = [
            status_row("Moteur", agent.db.label),
            status_row("Base", ("✓ " if db_ok else "✕ ") + db_msg, "rk-ok" if db_ok else "rk-ko"),
            status_row("Modèle", settings.llm_model),
            status_row("Serveur LLM", re.sub(r"^https?://", "", settings.llm_base_url)),
            status_row("LLM", ("✓ " if llm_ok else "✕ ") + llm_msg, "rk-ok" if llm_ok else "rk-ko"),
            status_row("Accès", "lecture seule"),
            status_row("Plafond", f"{settings.max_rows} lignes · {settings.query_timeout_s} s"),
        ]
        st.markdown("".join(rows), unsafe_allow_html=True)

        st.markdown('<div class="rk-side-title">Transparence</div>', unsafe_allow_html=True)
        with st.expander("Schéma vu par le LLM"):
            try:
                st.code(agent.schema_text(), language=None, wrap_lines=True)
            except Exception as exc:  # base injoignable au démarrage
                st.warning(f"Schéma indisponible : {exc}")
        st.caption("Données fictives · 100 % on-premise · chaque question est journalisée.")
    return clicked


# --- Page -----------------------------------------------------------------------
def main() -> None:
    # Favicon local : une icône « :material/...: » serait téléchargée depuis fonts.gstatic.com.
    st.set_page_config(
        page_title="RK Data · Chat Lakehouse", page_icon=str(ASSETS / "favicon.svg"), layout="wide"
    )
    st.markdown(CSS, unsafe_allow_html=True)
    agent = get_agent()
    st.session_state.setdefault("messages", [])

    clicked = render_sidebar(agent)

    st.markdown(
        f"""<div class="rk-header">
  <img src="{svg_data_uri('logo-rkdata-blanc.svg')}" alt="RK Data">
  <div>
    <div class="rk-title">Chat avec votre Lakehouse</div>
    <div class="rk-sub">Interrogez vos données en français · modèle hébergé chez vous · accès en lecture seule · requêtes auditées</div>
  </div>
</div>""",
        unsafe_allow_html=True,
    )

    # Le champ de saisie reste ancré en bas de page, où qu'il soit déclaré.
    question = st.chat_input("Posez votre question sur les données…") or clicked

    if not st.session_state.messages and not question:
        st.markdown(
            '<div class="rk-empty"><b>Posez une question métier en français.</b><br>'
            "L'assistant la traduit en SQL, l'exécute sur le lakehouse en lecture seule, puis vous "
            "rend une synthèse, un graphique, les données et la requête utilisée. "
            "Essayez un des exemples de la barre latérale.</div>",
            unsafe_allow_html=True,
        )

    for index, message in enumerate(st.session_state.messages):
        if message["role"] == "user":
            with st.chat_message("user", avatar=":material/person:"):
                st.markdown(message["content"].replace("$", "\\$"))
        else:
            with st.chat_message("assistant", avatar=":material/query_stats:"):
                render_answer(message["answer"], index)

    if not question:
        return

    # Mémoire courte : les couples question/SQL réussis servent aux relances.
    history = [
        {"question": m["answer"]["question"], "sql": m["answer"]["sql"]}
        for m in st.session_state.messages
        if m["role"] == "assistant" and m["answer"]["status"] == "ok"
    ]
    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user", avatar=":material/person:"):
        st.markdown(question.replace("$", "\\$"))
    with st.chat_message("assistant", avatar=":material/query_stats:"):
        with st.spinner("Analyse de la question, génération du SQL et exécution…"):
            answer = agent.ask(question, history=history, user=current_user(agent.settings)).to_dict()
        st.session_state.messages.append({"role": "assistant", "answer": answer})
        render_answer(answer, len(st.session_state.messages) - 1)


main()
