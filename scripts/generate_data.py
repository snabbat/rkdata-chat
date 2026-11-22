"""Génère le jeu de données de démonstration (banque de détail marocaine fictive).

Déterministe (graine 42) : deux exécutions produisent exactement les mêmes données.

Sorties :
  1. infra/seed/seed_trino.sql : schéma + tables Iceberg (avec COMMENT) + INSERT
     par lots de 1000 lignes, exécuté par le job `seed` via le CLI Trino.
  2. data/banque.db            : copie SQLite, pour le mode hors-ligne et les tests.

Les descriptions (COMMENT) proviennent de semantic/banque.yaml, source unique
partagée avec le prompt du LLM.

Usage : python scripts/generate_data.py
"""

from __future__ import annotations

import argparse
import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SEMANTIC_FILE = ROOT / "semantic" / "banque.yaml"
SEED_SQL_FILE = ROOT / "infra" / "seed" / "seed_trino.sql"
SQLITE_FILE = ROOT / "data" / "banque.db"

SEED = 42
BATCH_SIZE = 1000
SCHEMA = "banque"
SCHEMA_LOCATION = "s3://warehouse/banque"

N_CLIENTS = 2000
N_TRANSACTIONS = 30000
N_CREDITS = 800
TX_START = date(2025, 1, 1)
TX_END = date(2026, 10, 31)
REF_DATE = TX_END  # « aujourd'hui » du jeu de données

# --- Structure physique : (colonne, type SQL). Les descriptions sont dans le YAML.
TABLES: dict[str, list[tuple[str, str]]] = {
    "agences": [
        ("agence_id", "INTEGER"),
        ("nom_agence", "VARCHAR"),
        ("ville", "VARCHAR"),
        ("region", "VARCHAR"),
    ],
    "clients": [
        ("client_id", "INTEGER"),
        ("nom", "VARCHAR"),
        ("prenom", "VARCHAR"),
        ("segment", "VARCHAR"),
        ("date_entree", "DATE"),
        ("agence_id", "INTEGER"),
        ("ville", "VARCHAR"),
        ("revenu_mensuel", "DECIMAL(14,2)"),
    ],
    "comptes": [
        ("compte_id", "INTEGER"),
        ("client_id", "INTEGER"),
        ("type_compte", "VARCHAR"),
        ("devise", "VARCHAR"),
        ("date_ouverture", "DATE"),
        ("solde", "DECIMAL(16,2)"),
        ("statut", "VARCHAR"),
    ],
    "transactions": [
        ("transaction_id", "BIGINT"),
        ("compte_id", "INTEGER"),
        ("date_operation", "DATE"),
        ("montant", "DECIMAL(14,2)"),
        ("sens", "VARCHAR"),
        ("canal", "VARCHAR"),
        ("categorie", "VARCHAR"),
    ],
    "credits": [
        ("credit_id", "INTEGER"),
        ("client_id", "INTEGER"),
        ("type_credit", "VARCHAR"),
        ("montant_accorde", "DECIMAL(14,2)"),
        ("taux_annuel", "DECIMAL(5,2)"),
        ("duree_mois", "INTEGER"),
        ("date_octroi", "DATE"),
        ("encours", "DECIMAL(14,2)"),
        ("statut", "VARCHAR"),
    ],
}
# Propriétés Iceberg spécifiques (partitionnement).
TABLE_PROPERTIES = {
    "transactions": "partitioning = ARRAY['month(date_operation)']",
}

# --- Référentiels -----------------------------------------------------------
# (nom d'agence, ville, région, poids commercial)
AGENCES = [
    ("Casablanca Maârif", "Casablanca", "Casablanca-Settat", 2.2),
    ("Casablanca Anfa", "Casablanca", "Casablanca-Settat", 2.2),
    ("Casablanca Sidi Maârouf", "Casablanca", "Casablanca-Settat", 2.0),
    ("Casablanca Aïn Sebaâ", "Casablanca", "Casablanca-Settat", 1.8),
    ("Mohammedia Centre", "Mohammedia", "Casablanca-Settat", 1.2),
    ("El Jadida Plateau", "El Jadida", "Casablanca-Settat", 1.0),
    ("Settat Centre", "Settat", "Casablanca-Settat", 0.9),
    ("Rabat Agdal", "Rabat", "Rabat-Salé-Kénitra", 1.8),
    ("Rabat Hassan", "Rabat", "Rabat-Salé-Kénitra", 1.6),
    ("Salé Tabriquet", "Salé", "Rabat-Salé-Kénitra", 1.2),
    ("Kénitra Centre", "Kénitra", "Rabat-Salé-Kénitra", 1.1),
    ("Marrakech Guéliz", "Marrakech", "Marrakech-Safi", 1.7),
    ("Marrakech Médina", "Marrakech", "Marrakech-Safi", 1.2),
    ("Safi Centre", "Safi", "Marrakech-Safi", 0.9),
    ("Fès Ville Nouvelle", "Fès", "Fès-Meknès", 1.5),
    ("Meknès Hamria", "Meknès", "Fès-Meknès", 1.2),
    ("Taza Centre", "Taza", "Fès-Meknès", 0.7),
    ("Tanger Boulevard", "Tanger", "Tanger-Tétouan-Al Hoceïma", 1.7),
    ("Tétouan Centre", "Tétouan", "Tanger-Tétouan-Al Hoceïma", 1.0),
    ("Al Hoceïma Centre", "Al Hoceïma", "Tanger-Tétouan-Al Hoceïma", 0.7),
    ("Agadir Talborjt", "Agadir", "Souss-Massa", 1.5),
    ("Inezgane Centre", "Inezgane", "Souss-Massa", 0.9),
    ("Oujda Centre", "Oujda", "Oriental", 1.3),
    ("Béni Mellal Centre", "Béni Mellal", "Béni Mellal-Khénifra", 1.1),
    ("Laâyoune Centre", "Laâyoune", "Laâyoune-Sakia El Hamra", 0.9),
]

# Taux d'impayés cible par région (part de l'encours en Impayé ou Contentieux) :
# donne du relief à la question « taux d'impayés par région ».
RISQUE_REGION = {
    "Casablanca-Settat": 0.075,
    "Rabat-Salé-Kénitra": 0.060,
    "Marrakech-Safi": 0.125,
    "Fès-Meknès": 0.110,
    "Tanger-Tétouan-Al Hoceïma": 0.085,
    "Souss-Massa": 0.100,
    "Oriental": 0.145,
    "Béni Mellal-Khénifra": 0.135,
    "Laâyoune-Sakia El Hamra": 0.070,
}
# Propension relative au défaut par type de crédit (la consommation est la plus risquée).
RISQUE_TYPE = {"Immobilier": 0.7, "Consommation": 1.5, "Auto": 1.0, "Equipement": 1.0}

PRENOMS = [
    "Mohamed", "Ahmed", "Youssef", "Hamza", "Omar", "Ali", "Mehdi", "Karim", "Rachid",
    "Hassan", "Said", "Khalid", "Anas", "Amine", "Ismail", "Bilal", "Zakaria", "Tarik",
    "Nabil", "Driss", "Abdellah", "Mustapha", "Othmane", "Reda", "Adil",
    "Fatima", "Khadija", "Aicha", "Meryem", "Salma", "Zineb", "Nadia", "Laila", "Samira",
    "Hanane", "Imane", "Sara", "Houda", "Asmae", "Loubna", "Ghita", "Kenza", "Hajar",
    "Naima", "Souad", "Latifa", "Rim", "Yasmine", "Ikram", "Siham",
]
NOMS = [
    "Alaoui", "Bennani", "El Idrissi", "Tazi", "Berrada", "Benjelloun", "Chraibi", "Fassi",
    "Lahlou", "Sebti", "Kettani", "El Amrani", "Benkirane", "Squalli", "Bouzidi", "Naciri",
    "El Mansouri", "Ouazzani", "Cherkaoui", "Filali", "Belhaj", "Amrani", "Ziani",
    "El Khattabi", "Raji", "Mouline", "Sqalli", "Haddad", "Bensouda", "El Fassi",
    "Benali", "Daoudi", "El Ghazali", "Jabri", "Kabbaj", "Lamrani", "Mernissi", "Najib",
    "Ouali", "Rahmani", "Saidi", "Tahiri", "Zerouali", "Ait Ali", "Boukhari", "Chafik",
    "Dahbi", "El Bakkali", "Guessous", "Hilali",
]
SOCIETE_PREFIXES = [
    "Atlas", "Maghreb", "Al Baraka", "Rif", "Souss", "Anfa", "Oum Rabia", "Tensift",
    "Bouregreg", "Sahara", "Zenith", "Najah", "Al Amal", "Chellah", "Toubkal",
]
SOCIETE_ACTIVITES = [
    "Négoce", "Distribution", "Transport", "Bâtiment", "Agro", "Textile", "Logistique",
    "Services", "Industries", "Immobilier", "Conseil", "Équipements",
]
SOCIETE_FORMES = ["SARL", "SA", "SARL AU"]

# (catégorie, sens, poids, [(canal, poids)], montant médian en MAD)
CATEGORIES = [
    ("Salaire", "CREDIT", 10, [("Virement", 1)], None),  # montant = revenu du client
    ("Virement reçu", "CREDIT", 9, [("Virement", 5), ("Mobile", 3), ("Web", 2)], 3200),
    ("Versement espèces", "CREDIT", 6, [("Agence", 6), ("GAB", 4)], 2600),
    ("Retrait espèces", "DEBIT", 16, [("GAB", 8), ("Agence", 2)], 900),
    ("Paiement facture", "DEBIT", 12, [("Mobile", 5), ("Web", 3), ("Agence", 2)], 650),
    ("Achat", "DEBIT", 18, [("Web", 5), ("Mobile", 5)], 480),
    ("Virement émis", "DEBIT", 10, [("Virement", 4), ("Mobile", 4), ("Web", 2)], 2900),
    ("Remboursement crédit", "DEBIT", 5, [("Virement", 1)], 2800),
    ("Frais bancaires", "DEBIT", 4, [("Agence", 1)], 45),
]
# Les dépenses gonflent l'été et en fin d'année, les encaissements en début d'année.
SAISON_DEBIT = {7: 1.30, 8: 1.35, 12: 1.25, 4: 1.10}
SAISON_CREDIT = {1: 1.15, 3: 1.10, 9: 1.10}
FACTEUR_SEGMENT = {"Particulier": 1.0, "Professionnel": 3.0, "Entreprise": 12.0}


def wchoice(rng: random.Random, options: list[tuple]):
    """Tirage pondéré dans une liste de couples (valeur, poids)."""
    return rng.choices([o[0] for o in options], weights=[o[1] for o in options], k=1)[0]


def lognormal(rng: random.Random, median: float, sigma: float = 0.6) -> float:
    return median * rng.lognormvariate(0, sigma)


def random_date(rng: random.Random, start: date, end: date) -> date:
    return start + timedelta(days=rng.randint(0, max((end - start).days, 0)))


# --- Génération --------------------------------------------------------------
def generate(seed: int = SEED) -> dict[str, list[tuple]]:
    rng = random.Random(seed)

    agences = [(i + 1, nom, ville, region) for i, (nom, ville, region, _) in enumerate(AGENCES)]
    agence_weights = [a[3] for a in AGENCES]
    region_of_agence = {a[0]: a[3] for a in agences}
    villes = sorted({a[2] for a in agences})

    # Clients
    clients = []
    for client_id in range(1, N_CLIENTS + 1):
        segment = wchoice(rng, [("Particulier", 80), ("Professionnel", 14), ("Entreprise", 6)])
        agence = rng.choices(agences, weights=agence_weights, k=1)[0]
        if segment == "Entreprise":
            nom = f"{rng.choice(SOCIETE_PREFIXES)} {rng.choice(SOCIETE_ACTIVITES)} {rng.choice(SOCIETE_FORMES)}"
            prenom = None
            revenu = min(lognormal(rng, 350_000, 0.8), 6_000_000)
        elif segment == "Professionnel":
            nom, prenom = rng.choice(NOMS), rng.choice(PRENOMS)
            revenu = min(lognormal(rng, 32_000, 0.6), 400_000)
        else:
            nom, prenom = rng.choice(NOMS), rng.choice(PRENOMS)
            revenu = min(max(lognormal(rng, 8_500, 0.55), 3_000), 90_000)
        # Entrées en relation plus nombreuses sur les années récentes.
        anciennete = int(7300 * rng.random() ** 1.6) + 30
        date_entree = REF_DATE - timedelta(days=anciennete)
        ville = agence[2] if rng.random() < 0.9 else rng.choice(villes)
        clients.append(
            (client_id, nom, prenom, segment, date_entree, agence[0], ville, round(revenu, -2))
        )
    client_by_id = {c[0]: c for c in clients}

    # Comptes : un compte courant par client, épargne et devises selon le segment.
    comptes = []
    compte_id = 0
    for c in clients:
        client_id, segment, date_entree, revenu = c[0], c[3], c[4], c[7]
        types = ["Courant"]
        if rng.random() < (0.42 if segment == "Particulier" else 0.25):
            types.append("Epargne")
        if rng.random() < {"Particulier": 0.08, "Professionnel": 0.20, "Entreprise": 0.35}[segment]:
            types.append("Devises")
        for type_compte in types:
            compte_id += 1
            devise = wchoice(rng, [("EUR", 6), ("USD", 4)]) if type_compte == "Devises" else "MAD"
            date_ouverture = (
                date_entree if type_compte == "Courant" else random_date(rng, date_entree, REF_DATE)
            )
            statut = wchoice(
                rng,
                [("Actif", 90), ("Dormant", 6), ("Clôturé", 4)]
                if type_compte == "Courant"
                else [("Actif", 80), ("Dormant", 13), ("Clôturé", 7)],
            )
            if statut == "Clôturé":
                solde = 0.0
            elif type_compte == "Courant":
                # ~6 % de comptes courants à découvert.
                solde = revenu * (-rng.uniform(0.05, 0.4) if rng.random() < 0.06 else rng.uniform(0.2, 3.0))
            elif type_compte == "Epargne":
                solde = revenu * rng.uniform(2.0, 30.0)
            else:
                solde = revenu * rng.uniform(0.5, 8.0)
            comptes.append(
                (compte_id, client_id, type_compte, devise, date_ouverture, round(solde, 2), statut)
            )

    # Transactions : tirées sur les comptes non clôturés, triées par date pour que
    # chaque lot d'INSERT ne touche qu'une ou deux partitions mensuelles.
    eligibles = [co for co in comptes if co[6] != "Clôturé"]
    poids_type = {"Courant": 10.0, "Epargne": 1.5, "Devises": 1.0}
    poids_comptes = [poids_type[co[2]] * (0.05 if co[6] == "Dormant" else 1.0) for co in eligibles]
    jours = [TX_START + timedelta(days=i) for i in range((TX_END - TX_START).days + 1)]
    # Activité en légère croissance sur la période (+~25 % en 22 mois).
    poids_jours = [1.0 + 0.25 * i / len(jours) for i in range(len(jours))]
    dates_tx = sorted(rng.choices(jours, weights=poids_jours, k=N_TRANSACTIONS))
    poids_categories = [cat[2] for cat in CATEGORIES]

    transactions = []
    for i, jour in enumerate(dates_tx, start=1):
        compte = rng.choices(eligibles, weights=poids_comptes, k=1)[0]
        client = client_by_id[compte[1]]
        segment, revenu = client[3], client[7]
        categorie, sens, _, canaux, mediane = rng.choices(CATEGORIES, weights=poids_categories, k=1)[0]
        if categorie == "Salaire" and segment != "Particulier":
            categorie, mediane = "Virement reçu", 3200  # pas de salaire pour les pros/entreprises
        canal = wchoice(rng, canaux)
        if categorie == "Salaire":
            montant = revenu * rng.uniform(0.92, 1.08)
        elif categorie == "Frais bancaires":
            montant = lognormal(rng, mediane, 0.5)
        else:
            montant = lognormal(rng, mediane, 0.7) * FACTEUR_SEGMENT[segment]
        saison = SAISON_DEBIT if sens == "DEBIT" else SAISON_CREDIT
        montant *= saison.get(jour.month, 1.0)
        if categorie == "Retrait espèces":
            montant = max(100.0, round(montant, -2))  # billets : multiples de 100
        transactions.append(
            (i, compte[0], jour, max(round(montant, 2), 5.0), sens, canal, categorie)
        )

    # Crédits
    types_par_segment = {
        "Particulier": [("Immobilier", 35), ("Consommation", 40), ("Auto", 25)],
        "Professionnel": [("Equipement", 55), ("Immobilier", 20), ("Auto", 15), ("Consommation", 10)],
        "Entreprise": [("Equipement", 75), ("Immobilier", 25)],
    }
    parametres = {  # type -> (montant médian, taux min, taux max, durées possibles)
        "Immobilier": (750_000, 4.20, 5.80, [120, 180, 240, 300]),
        "Consommation": (55_000, 6.50, 9.50, [12, 24, 36, 48, 60, 72, 84]),
        "Auto": (180_000, 5.50, 7.50, [36, 48, 60, 72]),
        "Equipement": (600_000, 5.00, 7.00, [36, 60, 84, 120]),
    }
    credits = []
    for credit_id in range(1, N_CREDITS + 1):
        client = rng.choice(clients)
        client_id, segment, date_entree, agence_id = client[0], client[3], client[4], client[5]
        type_credit = wchoice(rng, types_par_segment[segment])
        mediane, taux_min, taux_max, durees = parametres[type_credit]
        facteur = {"Particulier": 1.0, "Professionnel": 1.5, "Entreprise": 4.0}[segment]
        montant = max(10_000.0, round(lognormal(rng, mediane * facteur, 0.5), -3))
        taux = round(round(rng.uniform(taux_min, taux_max) / 0.05) * 0.05, 2)
        duree = rng.choice(durees)
        # Crédit encore en vie : entre 1 mois et 95 % de sa durée déjà écoulés.
        mois_ecoules = rng.uniform(1.0, duree * 0.95)
        date_octroi = max(REF_DATE - timedelta(days=int(mois_ecoules * 30.44)), date_entree)
        mois_ecoules = (REF_DATE - date_octroi).days / 30.44
        encours = round(montant * max(0.0, 1.0 - mois_ecoules / duree), 2)
        credits.append(
            [credit_id, client_id, type_credit, montant, taux, duree, date_octroi, encours, "Sain"]
        )

    # Statut des crédits : dans chaque région, on fait basculer des crédits en défaut
    # jusqu'à atteindre le taux d'impayés cible (en part d'encours). Un tirage
    # indépendant par crédit donnerait des taux erratiques dans les petites régions.
    par_region: dict[str, list[list]] = {}
    for credit in credits:
        region = region_of_agence[client_by_id[credit[1]][5]]
        par_region.setdefault(region, []).append(credit)
    for region, credits_region in par_region.items():
        cible = RISQUE_REGION[region] * sum(c[7] for c in credits_region)
        # Seuls les crédits de plus de 4 mois peuvent être en défaut ; les types les
        # plus risqués passent en tête de file.
        candidats = [c for c in credits_region if (REF_DATE - c[6]).days >= 120]
        candidats.sort(key=lambda c: rng.random() / RISQUE_TYPE[c[2]])
        cumul = 0.0
        for credit in candidats:
            if cumul >= cible:
                break
            if cumul + credit[7] > cible * 1.10:
                continue  # trop gros : ferait dépasser la cible
            cumul += credit[7]
            credit[8] = "Contentieux" if rng.random() < 0.35 else "Impayé"
    credits = [tuple(c) for c in credits]

    return {
        "agences": agences,
        "clients": clients,
        "comptes": comptes,
        "transactions": transactions,
        "credits": credits,
    }


# --- Couche sémantique -------------------------------------------------------
def load_descriptions() -> dict:
    """Lit semantic/banque.yaml et vérifie que tout ce qui est créé y est décrit."""
    semantic = yaml.safe_load(SEMANTIC_FILE.read_text(encoding="utf-8"))
    for table, columns in TABLES.items():
        spec = semantic["tables"].get(table)
        if not spec or not spec.get("description"):
            raise SystemExit(f"semantic/banque.yaml : description manquante pour la table {table}")
        for column, _ in columns:
            if not spec["colonnes"].get(column):
                raise SystemExit(f"semantic/banque.yaml : description manquante pour {table}.{column}")
    return semantic


# --- Export Trino ------------------------------------------------------------
def sql_literal(value, sql_type: str) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, date):
        return f"DATE '{value.isoformat()}'"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if sql_type.startswith("DECIMAL"):
        return f"{value:.2f}"
    return str(int(value))


def quote(text: str) -> str:
    return "'" + " ".join(text.split()).replace("'", "''") + "'"


def write_trino_seed(data: dict[str, list[tuple]], semantic: dict, catalog: str) -> None:
    target = f"{catalog}.{SCHEMA}"
    out = [
        "-- Fichier généré par scripts/generate_data.py — ne pas modifier à la main.",
        f"CREATE SCHEMA IF NOT EXISTS {target} WITH (location = '{SCHEMA_LOCATION}');",
        "",
    ]
    for table, columns in TABLES.items():
        spec = semantic["tables"][table]
        cols_sql = ",\n".join(
            f"  {name} {sql_type} COMMENT {quote(spec['colonnes'][name])}" for name, sql_type in columns
        )
        properties = ["format = 'PARQUET'"]
        if table in TABLE_PROPERTIES:
            properties.append(TABLE_PROPERTIES[table])
        out.append(f"DROP TABLE IF EXISTS {target}.{table};")
        out.append(
            f"CREATE TABLE {target}.{table} (\n{cols_sql}\n)\n"
            f"COMMENT {quote(spec['description'])}\n"
            f"WITH ({', '.join(properties)});"
        )
        rows = data[table]
        for start in range(0, len(rows), BATCH_SIZE):
            values = ",\n".join(
                "(" + ", ".join(sql_literal(v, t) for v, (_, t) in zip(row, columns)) + ")"
                for row in rows[start : start + BATCH_SIZE]
            )
            out.append(f"INSERT INTO {target}.{table} VALUES\n{values};")
        out.append("")
    SEED_SQL_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEED_SQL_FILE.write_text("\n".join(out), encoding="utf-8", newline="\n")


# --- Export SQLite -----------------------------------------------------------
def write_sqlite(data: dict[str, list[tuple]]) -> None:
    SQLITE_FILE.parent.mkdir(parents=True, exist_ok=True)
    SQLITE_FILE.unlink(missing_ok=True)
    conn = sqlite3.connect(SQLITE_FILE)
    try:
        for table, columns in TABLES.items():
            # Mêmes noms de types que côté Trino ; les dates sont stockées en texte ISO.
            cols_sql = ", ".join(f"{name} {sql_type}" for name, sql_type in columns)
            conn.execute(f"CREATE TABLE {table} ({cols_sql})")
            placeholders = ", ".join("?" for _ in columns)
            conn.executemany(
                f"INSERT INTO {table} VALUES ({placeholders})",
                [tuple(v.isoformat() if isinstance(v, date) else v for v in row) for row in data[table]],
            )
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--catalog", default="iceberg", help="catalogue Trino cible (défaut : iceberg)")
    args = parser.parse_args()

    semantic = load_descriptions()
    data = generate()
    write_trino_seed(data, semantic, args.catalog)
    write_sqlite(data)

    print("Jeu de données généré (graine %d) :" % SEED)
    for table, rows in data.items():
        print(f"  {table:<13} {len(rows):>6} lignes")
    encours = sum(c[7] for c in data["credits"])
    impayes = sum(c[7] for c in data["credits"] if c[8] != "Sain")
    print(f"  taux d'impayés global : {100 * impayes / encours:.2f} %")
    print(f"  -> {SEED_SQL_FILE.relative_to(ROOT)} ({SEED_SQL_FILE.stat().st_size / 1e6:.1f} Mo)")
    print(f"  -> {SQLITE_FILE.relative_to(ROOT)} ({SQLITE_FILE.stat().st_size / 1e6:.1f} Mo)")


if __name__ == "__main__":
    main()
