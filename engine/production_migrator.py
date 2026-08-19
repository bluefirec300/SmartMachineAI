from __future__ import annotations
from config.environment import get_config_db_path

import argparse
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_DATABASE_PATH = get_config_db_path()

# Hierarchy: Product Category -> Product -> Batch/Production Run.
# Colour/variant is deliberately a PRODUCT-level field (products.variant),
# not its own category - "Red Base Coat" and "Black Base Coat" are two
# products under one "Base Coat" category, per explicit direction.
CREATE_PRODUCT_CATEGORIES_SQL = """
CREATE TABLE IF NOT EXISTS product_categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    created_at TEXT NOT NULL
)
"""

# unit_of_measure is deliberately free text ('kg'/'tonne'/'litre'/...),
# not an enum - this project never assumes tonnes-only. recipe_reference
# is a label/pointer only (e.g. a document/recipe ID) - no formulation
# ingredients are stored here or planned, per explicit direction.
# source distinguishes 'simulated' (Phase 4's seed catalog) from a
# future 'user_entered'/'imported' real catalog - same provenance
# convention used on production_batches below.
#
# No density/energy-intensity column is added here - this project's
# additive-migration pattern (used in every phase so far) already means
# either can be added later with zero redesign; adding an empty
# placeholder column now would just be unused schema.
CREATE_PRODUCTS_SQL = """
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_code TEXT NOT NULL UNIQUE,
    product_name TEXT NOT NULL,
    category_id INTEGER REFERENCES product_categories(id),
    variant TEXT,
    standard_batch_size REAL,
    unit_of_measure TEXT NOT NULL,
    recipe_reference TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL DEFAULT 'simulated',
    created_at TEXT NOT NULL
)
"""

# plant_id/area_id/system_id/equipment_id are all structured FKs into
# the Phase 1 hierarchy - never derived from name-string parsing at
# batch-creation time (the equipment row already carries these from
# Phase 1's backfill, so a batch just copies them). shift_id reuses
# Phase 2's shift_definitions - no duplicate shift system.
#
# unit_of_measure is stored again here (not just inherited from the
# product) so a batch's ACTUALLY recorded unit is preserved even on
# the rare occasion it differs from the product's own default unit.
#
# start_time/end_time are full datetimes ('YYYY-MM-DD HH:MM:SS'),
# precise enough for later energy-window correlation (Phase 5+) - not
# date-only.
CREATE_PRODUCTION_BATCHES_SQL = """
CREATE TABLE IF NOT EXISTS production_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_code TEXT NOT NULL UNIQUE,
    product_id INTEGER NOT NULL REFERENCES products(id),
    plant_id INTEGER NOT NULL REFERENCES plants(id),
    area_id INTEGER REFERENCES areas(id),
    system_id INTEGER REFERENCES systems(id),
    equipment_id INTEGER REFERENCES equipment(id),
    shift_id INTEGER REFERENCES shift_definitions(id),
    status TEXT NOT NULL DEFAULT 'planned',
    start_time TEXT,
    end_time TEXT,
    planned_quantity REAL,
    actual_quantity REAL,
    good_quantity REAL,
    reject_quantity REAL,
    unit_of_measure TEXT NOT NULL,
    downtime_minutes REAL,
    downtime_reason TEXT,
    source TEXT NOT NULL DEFAULT 'simulated',
    created_at TEXT NOT NULL
)
"""

# The roadmap's own "may include" example categories - not fabricated.
SEED_CATEGORIES = (
    "Base Coat", "Clear Coat", "Primer", "Thinner", "Hardener / Activator",
    "Sealer", "Additives", "Putty / Body Filler", "Specialty Coatings",
)

# (product_code, product_name, category, variant, standard_batch_size, unit_of_measure)
# Illustrative starter catalog - marked source='simulated', same as the
# batches generated against it. Units vary deliberately (litre for
# liquid coatings/thinners/hardeners/sealers, kg for powders/fillers) -
# this project never assumes every product is measured in tonnes.
SEED_PRODUCTS = (
    ("BC-RED-01", "Red Base Coat", "Base Coat", "Red", 200.0, "litre"),
    ("BC-BLK-01", "Black Base Coat", "Base Coat", "Black", 200.0, "litre"),
    ("BC-WHT-01", "White Base Coat", "Base Coat", "White", 200.0, "litre"),
    ("CC-STD-01", "Standard Clear Coat", "Clear Coat", None, 250.0, "litre"),
    ("CC-HS-01", "High Solids Clear Coat", "Clear Coat", None, 250.0, "litre"),
    ("PR-GRY-01", "Grey Primer", "Primer", "Grey", 180.0, "litre"),
    ("PR-EPX-01", "Epoxy Primer", "Primer", None, 180.0, "litre"),
    ("TH-STD-01", "Standard Thinner", "Thinner", None, 300.0, "litre"),
    ("TH-FAST-01", "Fast Evaporating Thinner", "Thinner", None, 300.0, "litre"),
    ("HD-FAST-01", "Fast Hardener", "Hardener / Activator", None, 100.0, "litre"),
    ("HD-SLOW-01", "Slow Hardener", "Hardener / Activator", None, 100.0, "litre"),
    ("SL-STD-01", "Standard Sealer", "Sealer", None, 220.0, "litre"),
    ("AD-FLEX-01", "Flex Additive", "Additives", None, 50.0, "kg"),
    ("AD-MATT-01", "Matting Additive", "Additives", None, 50.0, "kg"),
    ("PF-STD-01", "Standard Body Filler", "Putty / Body Filler", None, 500.0, "kg"),
    ("PF-FINE-01", "Fine Finishing Filler", "Putty / Body Filler", None, 400.0, "kg"),
    ("SC-PEARL-01", "Pearl Effect Coating", "Specialty Coatings", "Pearl", 150.0, "litre"),
    ("SC-MET-01", "Metallic Coating", "Specialty Coatings", "Metallic Silver", 150.0, "litre"),
    ("SC-CAND-01", "Candy Effect Coating", "Specialty Coatings", "Candy Red", 120.0, "litre"),
)


def _seed_categories(connection: sqlite3.Connection, now: str) -> dict[str, int]:
    category_ids: dict[str, int] = {}

    for name in SEED_CATEGORIES:
        row = connection.execute(
            "SELECT id FROM product_categories WHERE name = ?", (name,)
        ).fetchone()

        if row:
            category_ids[name] = row["id"]
            continue

        cursor = connection.execute(
            "INSERT INTO product_categories (name, description, created_at) VALUES (?, NULL, ?)",
            (name, now),
        )
        category_ids[name] = cursor.lastrowid

    return category_ids


def _seed_products(connection: sqlite3.Connection, category_ids: dict[str, int], now: str) -> int:
    added = 0

    for product_code, product_name, category_name, variant, batch_size, unit in SEED_PRODUCTS:
        existing = connection.execute(
            "SELECT id FROM products WHERE product_code = ?", (product_code,)
        ).fetchone()

        if existing:
            continue

        connection.execute(
            """
            INSERT INTO products (
                product_code, product_name, category_id, variant,
                standard_batch_size, unit_of_measure, recipe_reference,
                active, source, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, 1, 'simulated', ?)
            """,
            (product_code, product_name, category_ids[category_name], variant, batch_size, unit, now),
        )
        added += 1

    return added


def migrate(database: Path, backup: bool = True) -> None:
    database = database.expanduser().resolve()

    if not database.exists():
        raise FileNotFoundError(f"Database not found: {database}")

    if backup:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = database.with_name(
            f"{database.stem}_before_production_{stamp}.db"
        )
        shutil.copy2(database, backup_path)
        print(f"Backup created: {backup_path}")

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row

    try:
        connection.execute(CREATE_PRODUCT_CATEGORIES_SQL)
        connection.execute(CREATE_PRODUCTS_SQL)
        connection.execute(CREATE_PRODUCTION_BATCHES_SQL)
        connection.commit()
        print("product_categories/products/production_batches tables ready.")

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        category_ids = _seed_categories(connection, now)
        connection.commit()
        print(f"Product categories ready: {len(category_ids)}")

        added_products = _seed_products(connection, category_ids, now)
        connection.commit()
        total_products = connection.execute("SELECT COUNT(*) AS c FROM products").fetchone()["c"]
        print(f"Added {added_products} new product(s); {total_products} total.")

    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create product_categories/products/production_batches tables and seed a starter catalog.",
    )

    parser.add_argument("--database", default=str(DEFAULT_DATABASE_PATH))
    parser.add_argument("--no-backup", action="store_true")

    args = parser.parse_args()

    migrate(Path(args.database), backup=not args.no_backup)


if __name__ == "__main__":
    main()
