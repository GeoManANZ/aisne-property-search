#!/usr/bin/env python3
"""Final verification of aisne-recommendations-weekly.md rows."""
import re
import sqlite3
from pathlib import Path

HERE = Path(__file__).parent
md = (HERE / "aisne-recommendations-weekly.md").read_text(encoding="utf-8")
urls = re.findall(r"https://\S+", md)
print(f"URLs in file: {len(urls)}")
con = sqlite3.connect(HERE / "listings.db")
con.row_factory = sqlite3.Row
LAND_FEAT = re.compile(r"m² de terrain\s*([\s\d\u202f\u00a0,\.]+?)\s*m²", re.I)
TERRAIN_AD = re.compile(
    r"terrain\s*(?:à|a)\s*b[aâ]tir|terrain\s*de\s*loisirs|terrain\s*constructible", re.I)
problems = 0
seen = set()
for u in urls:
    if u in seen:
        print(f"DUPLICATE URL: {u}")
        problems += 1
    seen.add(u)
    r = con.execute("SELECT status, surface_m2, features, title, description, location "
                    "FROM listings WHERE url=?", (u,)).fetchone()
    if r is None:
        print(f"NOT IN DB: {u}"); problems += 1; continue
    if r["status"] != "active":
        print(f"STATUS={r['status']}: {u}"); problems += 1; continue
    blob = " ".join([r["title"] or "", r["description"] or "", r["features"] or ""])
    m = LAND_FEAT.search(r["features"] or "")
    land_eq = m and abs(float((m.group(1).replace("\u202f", "").replace("\u00a0", "")
                  .replace(" ", "").replace(",", ".")) or -1) - r["surface_m2"]) < 1.0
    if TERRAIN_AD.search(blob) or land_eq:
        print(f"LAND ROW STILL PRESENT: {u}"); problems += 1; continue
    note = "land==surface!" if land_eq else "ok"
    print(f'ok {r["location"][:30]:30} {r["surface_m2"]:>7.0f} m²  {note}')
print("PROBLEMS:", problems)
