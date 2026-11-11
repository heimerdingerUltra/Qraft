"""Base de démonstration « ventes » (DuckDB), pour tester le harnais sans données réelles.

Les données sont générées de façon déterministe. Une règle maison n'apparaît pas
dans le schéma, comme dans une vraie entreprise : le chiffre d'affaires ne compte
que les commandes au statut « payee » (les commandes « test » et « annulee » en sont exclues).
"""

from __future__ import annotations

import csv
import random
import tempfile
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

DDL = [
    "CREATE TABLE regions (region_id INTEGER PRIMARY KEY, code VARCHAR NOT NULL, nom VARCHAR NOT NULL)",
    "CREATE TABLE clients (client_id INTEGER PRIMARY KEY, nom VARCHAR NOT NULL, "
    "region_id INTEGER NOT NULL REFERENCES regions(region_id), segment VARCHAR NOT NULL, date_creation DATE NOT NULL)",
    "CREATE TABLE produits (produit_id INTEGER PRIMARY KEY, nom VARCHAR NOT NULL, categorie VARCHAR NOT NULL, "
    "prix_catalogue DECIMAL(10,2) NOT NULL)",
    "CREATE TABLE commandes (commande_id INTEGER PRIMARY KEY, client_id INTEGER NOT NULL REFERENCES clients(client_id), "
    "date_commande DATE NOT NULL, statut VARCHAR NOT NULL, canal VARCHAR NOT NULL)",
    "CREATE TABLE lignes_commande (commande_id INTEGER NOT NULL REFERENCES commandes(commande_id), "
    "produit_id INTEGER NOT NULL REFERENCES produits(produit_id), quantite INTEGER NOT NULL, "
    "prix_unitaire DECIMAL(10,2) NOT NULL, PRIMARY KEY (commande_id, produit_id))",
    "CREATE TABLE paiements (paiement_id INTEGER PRIMARY KEY, commande_id INTEGER NOT NULL REFERENCES commandes(commande_id), "
    "montant DECIMAL(10,2) NOT NULL, date_paiement DATE NOT NULL, moyen VARCHAR NOT NULL)",
]

REGIONS = [
    (1, "IDF", "Île-de-France"),
    (2, "ARA", "Auvergne-Rhône-Alpes"),
    (3, "NAQ", "Nouvelle-Aquitaine"),
    (4, "OCC", "Occitanie"),
    (5, "HDF", "Hauts-de-France"),
    (6, "PAC", "Provence-Alpes-Côte d'Azur"),
]

PRODUCTS = {
    "Informatique": (["Ordinateur portable 14", "Ordinateur portable 16", "Écran 24", "Écran 27", "Clavier",
                      "Souris", "Station d'accueil", "Casque audio"], (20, 1800)),
    "Mobilier": (["Bureau assis-debout", "Chaise ergonomique", "Armoire", "Caisson", "Table de réunion",
                  "Lampe de bureau"], (40, 900)),
    "Fournitures": (["Ramette de papier", "Boîte de stylos", "Classeurs", "Cartouche d'encre", "Agenda",
                     "Notes adhésives"], (3, 60)),
    "Services": (["Installation sur site", "Maintenance annuelle", "Formation", "Support premium"], (150, 2500)),
}

SURNAMES = ["Durand", "Martin", "Bernard", "Petit", "Robert", "Richard", "Moreau", "Laurent", "Lefèvre", "Garnier",
            "Faure", "Rousseau", "Blanc", "Guerin", "Muller", "Henry", "Roussel", "Nicolas", "Perrin", "Morin"]
FORMS = ["SARL", "SAS", "SA", "Conseil", "Industries", "Services", "& Fils", "Groupe"]
SEGMENTS = (["PME", "Grand compte", "Particulier"], [0.5, 0.15, 0.35])
STATUSES = (["payee", "annulee", "en_attente", "test"], [0.80, 0.08, 0.07, 0.05])
CHANNELS = (["web", "magasin", "telephone"], [0.5, 0.3, 0.2])
PAYMENT_METHODS = ["carte", "virement", "prelevement"]

CENT = Decimal("0.01")


def _money(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def _random_date(rng: random.Random, start: date, end: date) -> date:
    return start + timedelta(days=rng.randrange((end - start).days + 1))


def build_demo_db(path: Path, *, seed: int = 42, n_clients: int = 180, n_orders: int = 2600) -> Path:
    """Crée (ou recrée) la base de démonstration et retourne son chemin."""
    import duckdb

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    for stale in (path, path.with_name(path.name + ".wal")):
        if stale.exists():
            stale.unlink()

    rng = random.Random(seed)
    clients = []
    for i in range(1, n_clients + 1):
        name = f"{rng.choice(SURNAMES)} {rng.choice(FORMS)} {i:03d}"
        region = rng.choice(REGIONS)[0]
        segment = rng.choices(*SEGMENTS)[0]
        created = _random_date(rng, date(2023, 1, 1), date(2025, 12, 31))
        clients.append((i, name, region, segment, created))

    products = []
    product_id = 0
    for category, (names, (low, high)) in PRODUCTS.items():
        for name in names:
            product_id += 1
            products.append((product_id, name, category, _money(rng.uniform(low, high))))

    # Environ 12 % de clients « dormants » : aucune commande en 2025 (utile aux questions de négation).
    dormant = {c[0] for c in clients if c[4] < date(2025, 1, 1) and rng.random() < 0.12}

    orders, lines, payments = [], [], []
    payment_id = 0
    for order_id in range(1, n_orders + 1):
        ordered_on = _random_date(rng, date(2024, 1, 1), date(2025, 12, 31))
        for _ in range(50):
            client = rng.choice(clients)
            if client[4] <= ordered_on and not (ordered_on.year == 2025 and client[0] in dormant):
                break
        else:  # aucun client éligible trouvé : on décale la commande à la création d'un client actif
            client = rng.choice([c for c in clients if c[0] not in dormant])
            ordered_on = max(ordered_on, client[4])
        status = rng.choices(*STATUSES)[0]
        orders.append((order_id, client[0], ordered_on, status, rng.choices(*CHANNELS)[0]))

        total = Decimal("0")
        for product in rng.sample(products, rng.randint(1, 4)):
            quantity = rng.randint(1, 2) if product[2] == "Services" else rng.randint(1, 10)
            discount = Decimal(str(rng.choice([0, 0, 0, 0.05, 0.10, 0.15])))
            unit_price = _money(product[3] * (1 - discount))
            lines.append((order_id, product[0], quantity, unit_price))
            total += unit_price * quantity

        if status == "payee":
            if rng.random() < 0.8:
                payment_id += 1
                payments.append((payment_id, order_id, _money(total),
                                 ordered_on + timedelta(days=rng.randint(0, 25)), rng.choice(PAYMENT_METHODS)))
            else:
                first = _money(total * Decimal("0.3"))
                for amount, delay in ((first, rng.randint(0, 10)), (_money(total) - first, rng.randint(15, 45))):
                    payment_id += 1
                    payments.append((payment_id, order_id, amount, ordered_on + timedelta(days=delay),
                                     rng.choice(PAYMENT_METHODS)))
        elif status == "en_attente" and rng.random() < 0.3:
            payment_id += 1
            payments.append((payment_id, order_id, _money(total * Decimal("0.3")),
                             ordered_on + timedelta(days=rng.randint(0, 10)), rng.choice(PAYMENT_METHODS)))

    tables = {
        "regions": REGIONS,
        "clients": clients,
        "produits": products,
        "commandes": orders,
        "lignes_commande": lines,
        "paiements": payments,
    }
    con = duckdb.connect(str(path))
    try:
        for statement in DDL:
            con.execute(statement)
        # Chargement par fichiers CSV temporaires : bien plus rapide qu'un INSERT ligne à ligne.
        with tempfile.TemporaryDirectory() as tmp:
            for name, rows in tables.items():
                csv_path = Path(tmp) / f"{name}.csv"
                with csv_path.open("w", encoding="utf-8", newline="") as handle:
                    csv.writer(handle).writerows(rows)
                con.execute(f"COPY {name} FROM '{csv_path.as_posix()}' (HEADER false)")
        con.execute("CHECKPOINT")
    finally:
        con.close()
    return path
