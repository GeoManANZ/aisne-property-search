#!/usr/bin/env python3
"""Load the MTE/ANIL "Carte des loyers" rent indicators into listings.db.

WHY THIS EXISTS
The SeLoger RENT passes have been blocked by DataDome on every Webshare French
sticky IP since 2026-09-20. Verified 2026-09-29: all THREE sessions fail (fr-2
challenged outright, fr-1/fr-3 load the page but the BFF POST returns 403 which
parses as "0 ids, total None"). The "decays over hours" model was simply wrong —
10 days and counting. The rent retry cron was therefore printing a negative
result on every run while still spending metered bytes.

This is the researched replacement: the French Ministry of Housing (SDES/MTE)
publishes commune-level rent indicators built from ~9M leboncoin + SeLoger + PAP
rental ads — the same market we were trying to scrape, already aggregated, with
per-commune quality flags. Free, DIRECT egress, zero proxy cost, no captcha, and
it cannot be blocked.

LICENCE: usable freely with attribution —
  "Estimations ANIL, à partir des données du Groupe SeLoger et de leboncoin"

FILES (data_loyers/pred-*-mef-dhup.csv), ';' separated, FRENCH decimal commas:
  id_zone;INSEE_C;LIBGEO;EPCI;DEP;REG;loypredm2;lwr.IPm2;upr.IPm2;
  TYPPRED;nbobs_com;nbobs_mail;R2_adj
"""
from __future__ import annotations

import csv
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
DATA = ROOT / "data_loyers"
DB = ROOT / "listings.db"
YEAR = "2025"

# file -> kind label stored in the table
KINDS = {
    "pred-mai-mef-dhup.csv": "maison",
    "pred-app3-mef-dhup.csv": "app3",
    "pred-app12-mef-dhup.csv": "app12",
    "pred-app-mef-dhup.csv": "app",
}

DDL = """
CREATE TABLE IF NOT EXISTS rent_benchmarks (
    kind       TEXT NOT NULL,           -- maison | app3 | app12 | app
    insee      TEXT NOT NULL,
    commune    TEXT,
    dept       TEXT,
    year       TEXT,
    loy_m2     REAL,                    -- predicted EUR/m2/month, charges comprises
    loy_lo     REAL,                    -- lower bound of prediction interval
    loy_hi     REAL,                    -- upper bound of prediction interval
    typpred    TEXT,                    -- 'commune' = commune-level fit, 'maille' = wider mesh
    nbobs_com  INTEGER,                 -- ads observed in this commune
    nbobs_mail INTEGER,                 -- ads observed in the mesh
    r2_adj     REAL,                    -- model fit; <0.5 means treat with caution
    loaded_at  TEXT,
    PRIMARY KEY (kind, insee)
);
CREATE INDEX IF NOT EXISTS idx_rent_bench_dept ON rent_benchmarks(dept, kind);
"""


def num(s: str | None):
    """French CSV number: '8,95647852540627' -> 8.95647852540627."""
    if s is None:
        return None
    t = s.strip().strip('"').replace("\u00a0", "").replace(" ", "")
    if not t:
        return None
    t = t.replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def txt(s: str | None) -> str:
    return (s or "").strip().strip('"')


def load_file(path: Path, kind: str, dept_filter: str | None) -> list[tuple]:
    rows = []
    # NOT utf-8: data.gouv.fr ships these CSVs in cp1252/latin-1 (commune names
    # carry French accents, byte 0xe8 = 'è'). utf-8-sig raises UnicodeDecodeError.
    with path.open(encoding="cp1252", errors="replace", newline="") as fh:
        for r in csv.DictReader(fh, delimiter=";"):
            dep = txt(r.get("DEP"))
            if dept_filter and dep != dept_filter:
                continue
            rows.append((
                kind,
                txt(r.get("INSEE_C")),
                txt(r.get("LIBGEO")),
                dep,
                YEAR,
                num(r.get("loypredm2")),
                num(r.get("lwr.IPm2")),
                num(r.get("upr.IPm2")),
                txt(r.get("TYPPRED")),
                int(num(r.get("nbobs_com")) or 0),
                int(num(r.get("nbobs_mail")) or 0),
                num(r.get("R2_adj")),
            ))
    return rows


def main() -> int:
    dept = sys.argv[1] if len(sys.argv) > 1 else "02"
    con = sqlite3.connect(DB)
    con.executescript(DDL)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    total = 0
    for fname, kind in KINDS.items():
        p = DATA / fname
        if not p.exists():
            print(f"  MISSING {fname} — download it first")
            continue
        rows = load_file(p, kind, dept)
        con.executemany(
            "INSERT OR REPLACE INTO rent_benchmarks "
            "(kind,insee,commune,dept,year,loy_m2,loy_lo,loy_hi,typpred,"
            " nbobs_com,nbobs_mail,r2_adj,loaded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(r + (now,)) for r in rows])
        total += len(rows)
        print(f"  {kind:7s} {len(rows):4d} communes in dept {dept}  ({fname})")
    con.commit()
    print(f"\nLOADED: {total} rent benchmark rows (dept {dept}, {YEAR})\n")

    print("=== rent/m² by kind (dept {0}) ===".format(dept))
    for row in con.execute(
            "SELECT kind, COUNT(*), ROUND(AVG(loy_m2),2), ROUND(MIN(loy_m2),2), "
            "ROUND(MAX(loy_m2),2) FROM rent_benchmarks WHERE dept=? GROUP BY kind", (dept,)):
        print(f"  {row[0]:7s} n={row[1]:4d}  avg €{row[2]}/m²  min €{row[3]}  max €{row[4]}")

    print("\n=== quality: how trustworthy are these? ===")
    for row in con.execute(
            "SELECT kind, SUM(r2_adj < 0.5), SUM(nbobs_com < 30), SUM(typpred='maille'), COUNT(*) "
            "FROM rent_benchmarks WHERE dept=? GROUP BY kind", (dept,)):
        print(f"  {row[0]:7s} low-R2={row[1]:4d}  thin-sample(<30 obs)={row[2]:4d}  "
              f"mesh-not-commune={row[3]:4d}  of {row[4]}")

    print("\n=== sample: Aisne towns with the most observed ads (maison) ===")
    for row in con.execute(
            "SELECT commune, ROUND(loy_m2,2), ROUND(loy_lo,2), ROUND(loy_hi,2), "
            "nbobs_com, ROUND(r2_adj,2), typpred FROM rent_benchmarks "
            "WHERE dept=? AND kind='maison' ORDER BY nbobs_com DESC LIMIT 10", (dept,)):
        print(f"  {row[0][:24]:24s} €{row[1]}/m²  [{row[2]}–{row[3]}]  "
              f"{row[4]:4d} obs  R²={row[5]}  ({row[6]})")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
