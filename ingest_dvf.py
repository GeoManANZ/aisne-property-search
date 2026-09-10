#!/usr/bin/env python3
"""DVF ingest — the official French real-estate SALES register.

Source: Etalab "Demandes de valeurs foncières" (geo-dvf), i.e. the notarial
transaction records. Every sale, with date, price, address, commune, GPS and
building details. Open data (Licence Ouverte), no key, direct egress — costs
ZERO Webshare bandwidth (~0.8 MB per department-year).

Layout: https://files.data.gouv.fr/geo-dvf/latest/csv/<YEAR>/departements/<DEPT>.csv.gz

WHY THIS EXISTS
Tracks what actually sells and where, so asking prices (our scraped listings)
can later be compared against achieved prices, and so commune-level sold
€/m² can be interrogated.

DVF quirks handled here
  * One mutation (sale) = MANY rows: one per local/parcel. `valeur_fonciere`
    repeats on every row, so SUM(valeur_fonciere) is wrong and
    SUM(surface_reelle_bati) double-counts when a local is repeated across
    nature_culture codes. Hence: raw rows stored verbatim, plus dedupe views
    that take MAX(price) and SUM(deduped surfaces) per mutation.
  * Rows can be land-only (type_local empty) — kept, filtered in the views.
  * Re-running is idempotent: each row is hashed and INSERTed with OR IGNORE.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

BASE = "https://files.data.gouv.fr/geo-dvf/latest/csv/{year}/departements/{dept}.csv.gz"
DB = "listings.db"
CACHE = "data/dvf"

# Columns we keep (subset of DVF's stable schema).
COLS = [
    "id_mutation", "date_mutation", "numero_disposition", "nature_mutation",
    "valeur_fonciere", "adresse_numero", "adresse_suffixe", "adresse_nom_voie",
    "code_postal", "code_commune", "nom_commune", "code_departement",
    "id_parcelle", "lot1_numero", "lot1_surface_carrez", "nombre_lots",
    "code_type_local", "type_local", "surface_reelle_bati",
    "nombre_pieces_principales", "code_nature_culture", "nature_culture",
    "surface_terrain", "longitude", "latitude",
]
NUMERIC = {"valeur_fonciere", "surface_reelle_bati", "surface_terrain",
           "longitude", "latitude", "lot1_surface_carrez", "nombre_lots",
           "nombre_pieces_principales"}

DDL = """
CREATE TABLE IF NOT EXISTS dvf_sales (
    row_hash TEXT PRIMARY KEY,
    id_mutation TEXT, date_mutation TEXT, numero_disposition TEXT,
    nature_mutation TEXT, valeur_fonciere REAL,
    adresse_numero TEXT, adresse_suffixe TEXT, adresse_nom_voie TEXT,
    code_postal TEXT, code_commune TEXT, nom_commune TEXT, code_departement TEXT,
    id_parcelle TEXT, lot1_numero TEXT, lot1_surface_carrez REAL, nombre_lots INTEGER,
    code_type_local TEXT, type_local TEXT, surface_reelle_bati REAL,
    nombre_pieces_principales INTEGER,
    code_nature_culture TEXT, nature_culture TEXT,
    surface_terrain REAL, longitude REAL, latitude REAL,
    src_year INTEGER, ingested_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_dvf_mut ON dvf_sales(id_mutation);
CREATE INDEX IF NOT EXISTS ix_dvf_commune ON dvf_sales(nom_commune);
CREATE INDEX IF NOT EXISTS ix_dvf_date ON dvf_sales(date_mutation);
CREATE INDEX IF NOT EXISTS ix_dvf_type ON dvf_sales(type_local);

CREATE TABLE IF NOT EXISTS dvf_ingest_log (
    dept TEXT, year INTEGER, rows_read INTEGER, rows_inserted INTEGER,
    ingested_at TEXT, PRIMARY KEY (dept, year)
);
"""

# Derived data is MATERIALIZED into tables, not views: the per-mutation
# aggregation needs a correlated subquery over the land rows, and re-running
# that inside every report query blew past a 180 s timeout. Rebuilt after each
# ingest; reports interrogate the tables directly.
#
# DVF emits the SAME local once per nature_culture code, with a DIFFERENT
# surface_terrain on each row, so two dedupe passes are required:
#   b: building rows keyed WITHOUT surface_terrain, so a local counts once
#      (verified: mutation 2024-12830 -> 141 m2, not 282 m2)
#   l: land rows keyed on parcelle+culture, summed separately
REBUILD_DDL = """
DROP VIEW  IF EXISTS v_dvf_buildings;
DROP VIEW  IF EXISTS v_dvf_sales_clean;
DROP TABLE IF EXISTS dvf_buildings;
DROP TABLE IF EXISTS dvf_sales_clean;
DROP TABLE IF EXISTS _dvf_b;
DROP TABLE IF EXISTS _dvf_l;
DROP TABLE IF EXISTS _dvf_land;

-- Staged materialisation, NOT one nested query. The single-statement version
-- used a correlated subquery per mutation over a DISTINCT CTE, which re-scanned
-- 150k rows per mutation (O(n^2)) and was killed after 400 s. Each stage below
-- is a plain GROUP BY over an indexed table and the whole rebuild runs in ~1 s.

-- stage 1: building rows, deduped WITHOUT surface_terrain so a local counts once
--          (verified: mutation 2024-12830 -> 141 m2, not 282 m2)
CREATE TABLE _dvf_b AS
SELECT DISTINCT id_mutation, date_mutation, nature_mutation, valeur_fonciere,
       code_postal, nom_commune, code_commune, code_departement,
       type_local, id_parcelle, COALESCE(lot1_numero, '') AS lot,
       COALESCE(surface_reelle_bati, 0) AS srb,
       COALESCE(nombre_pieces_principales, 0) AS pieces,
       longitude, latitude
FROM dvf_sales
WHERE type_local IN ('Maison', 'Appartement', 'Immeuble');
CREATE INDEX IF NOT EXISTS ix_t_dvfb_mut ON _dvf_b(id_mutation);

-- stage 2: land rows, deduped on parcelle+culture (a parcelle appears once per
--          culture code with a different area, so these must not be summed raw)
CREATE TABLE _dvf_l AS
SELECT DISTINCT id_mutation, id_parcelle,
       COALESCE(code_nature_culture, '') AS culture, surface_terrain
FROM dvf_sales
WHERE surface_terrain IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_t_dvfl_mut ON _dvf_l(id_mutation);

-- stage 3: land total per mutation
CREATE TABLE _dvf_land AS
SELECT id_mutation, SUM(surface_terrain) AS surface_terrain
FROM _dvf_l GROUP BY id_mutation;
CREATE UNIQUE INDEX IF NOT EXISTS ix_t_land_mut ON _dvf_land(id_mutation);

-- stage 4: one row per mutation (sale)
CREATE TABLE dvf_buildings AS
SELECT b.id_mutation,
       MAX(b.date_mutation)    AS date_mutation,
       MAX(b.nature_mutation)  AS nature_mutation,
       MAX(b.valeur_fonciere)  AS prix_vente,
       MAX(b.code_postal)      AS code_postal,
       MAX(b.nom_commune)      AS commune,
       MAX(b.code_commune)     AS code_insee,
       MAX(b.code_departement) AS dept,
       COUNT(*)                AS n_locaux,
       SUM(b.srb)              AS surface_bati,
       CASE WHEN COUNT(DISTINCT b.type_local) = 1
            THEN MAX(b.type_local) ELSE 'Mixte' END AS type_local,
       SUM(b.pieces)           AS pieces,
       MAX(ld.surface_terrain) AS surface_terrain,
       AVG(b.longitude)        AS longitude,
       AVG(b.latitude)         AS latitude
FROM _dvf_b b
LEFT JOIN _dvf_land ld ON ld.id_mutation = b.id_mutation
GROUP BY b.id_mutation;

CREATE UNIQUE INDEX IF NOT EXISTS ix_dvfb_mut  ON dvf_buildings(id_mutation);
CREATE INDEX        IF NOT EXISTS ix_dvfb_comm ON dvf_buildings(commune);
CREATE INDEX        IF NOT EXISTS ix_dvfb_date ON dvf_buildings(date_mutation);

-- stage 5: the interrogable "what sold where" table (building sales only)
CREATE TABLE dvf_sales_clean AS
SELECT date_mutation, commune, code_postal, dept, type_local,
       prix_vente, ROUND(surface_bati) AS surface_bati,
       CASE WHEN surface_bati > 0 THEN ROUND(prix_vente / surface_bati)
            ELSE NULL END AS eur_m2,
       surface_terrain, n_locaux, pieces, longitude, latitude, id_mutation
FROM dvf_buildings
WHERE prix_vente > 0
  AND nature_mutation = 'Vente'
  AND surface_bati > 0;

CREATE INDEX IF NOT EXISTS ix_dvfc_comm ON dvf_sales_clean(commune);
CREATE INDEX IF NOT EXISTS ix_dvfc_date ON dvf_sales_clean(date_mutation);
CREATE INDEX IF NOT EXISTS ix_dvfc_type ON dvf_sales_clean(type_local);

DROP TABLE IF EXISTS _dvf_b;
DROP TABLE IF EXISTS _dvf_l;
DROP TABLE IF EXISTS _dvf_land;
"""

def fetch(dept: str, year: int, force: bool = False) -> str:
    """Download (or reuse cached) one department-year file."""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"{dept}-{year}.csv.gz")
    if os.path.exists(path) and os.path.getsize(path) > 1000 and not force:
        return path
    url = BASE.format(year=year, dept=dept)
    req = urllib.request.Request(url, headers={"User-Agent": "aisne-dvf-ingest/1.0"})
    print(f"  downloading {url}")
    try:
        with urllib.request.urlopen(req, timeout=180) as r, open(path, "wb") as fh:
            while chunk := r.read(1 << 16):
                fh.write(chunk)
    except urllib.error.HTTPError as e:
        print(f"    ! HTTP {e.code} for {dept}/{year} — skipping")
        if os.path.exists(path):
            os.remove(path)
        return ""
    print(f"    -> {path} ({os.path.getsize(path)/1048576:.1f} MB)")
    return path

def to_num(v: str):
    if v is None or v == "":
        return None
    try:
        f = float(v.replace(",", "."))
        return int(f) if f.is_integer() else f
    except ValueError:
        return None

def ingest_file(db: sqlite3.Connection, path: str, year: int) -> tuple[int, int]:
    read = ins = 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with gzip.open(path, "rt", encoding="utf-8", errors="replace", newline="") as fh:
        for row in csv.DictReader(fh):
            read += 1
            rec = {c: row.get(c, "") for c in COLS}
            h = hashlib.sha1("|".join(str(rec[c]) for c in COLS).encode()).hexdigest()
            vals = [rec[c] if c in NUMERIC else (rec[c] or None) for c in COLS]
            for i, c in enumerate(COLS):
                if c in NUMERIC:
                    vals[i] = to_num(rec[c]) if isinstance(vals[i], str) else vals[i]
            try:
                cur = db.execute(
                    f"INSERT OR IGNORE INTO dvf_sales (row_hash,{','.join(COLS)},"
                    f"src_year,ingested_at) VALUES (?{',?' * len(COLS)},?,?)",
                    [h, *vals, year, now],
                )
                ins += cur.rowcount
            except sqlite3.Error as e:
                print(f"    ! row error: {e}")
    return read, ins

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--depts", default="02",
                    help="comma-separated departments (default 02 = Aisne)")
    ap.add_argument("--years", default="2021,2022,2023,2024,2025")
    ap.add_argument("--force", action="store_true", help="re-download files")
    args = ap.parse_args()

    depts = [d.strip() for d in args.depts.split(",") if d.strip()]
    years = [int(y) for y in args.years.split(",") if y.strip()]

    db = sqlite3.connect(DB)
    db.executescript(DDL)
    db.commit()

    grand = 0
    for dept in depts:
        for year in years:
            path = fetch(dept, year, args.force)
            if not path:
                continue
            read, ins = ingest_file(db, path, year)
            db.execute(
                "INSERT OR REPLACE INTO dvf_ingest_log VALUES (?,?,?,?,?)",
                (dept, year, read, ins,
                 datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
            db.commit()
            print(f"  dept {dept} {year}: read {read:,} rows, inserted {ins:,} new")
            grand += ins

    print("\nrebuilding derived tables (dvf_buildings, dvf_sales_clean)...")
    db.executescript(REBUILD_DDL)
    db.commit()

    print(f"\ntotal new rows: {grand:,}")
    n_raw = db.execute("SELECT COUNT(*) FROM dvf_sales").fetchone()[0]
    n_sales = db.execute("SELECT COUNT(*) FROM dvf_sales_clean").fetchone()[0]
    span = db.execute("SELECT MIN(date_mutation), MAX(date_mutation) FROM dvf_sales_clean").fetchone()
    print(f"dvf_sales (raw rows):       {n_raw:,}")
    print(f"dvf_sales_clean (sales):    {n_sales:,}   period: {span[0]} -> {span[1]}")
    for r in db.execute(
        "SELECT type_local, COUNT(*), ROUND(AVG(eur_m2)) FROM dvf_sales_clean "
        "GROUP BY 1 ORDER BY 2 DESC"
    ):
        print(f"   {r[0]:12s} {r[1]:>6,} sales   avg {r[2]} EUR/m2")
    db.close()
    return 0

if __name__ == "__main__":
    sys.exit(main())
