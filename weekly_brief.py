#!/usr/bin/env python3
"""Rebuild aisne-recommendations-weekly.md excluding rows whose stored surface_m2
is the LAND (terrain) surface or that are outright land adverts.

Method: copy listings.db -> .weekly_tmp.db, set status='excluded_land_surface' on the
copy for (a) rows whose features state 'm² de terrain' equal to surface_m2, and
(b) rows advertising terrain à bâtir / terrain de loisirs / terrain constructible.
Then run category_brief.py against the copy. The live DB is never modified.
"""
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
SRC = HERE / "listings.db"
TMP = HERE / ".weekly_tmp.db"
OUT = HERE / "aisne-recommendations-weekly.md"

TERRAIN_AD = re.compile(
    r"terrain\s*(?:à|a)\s*b[aâ]tir|terrain\s*de\s*loisirs|terrain\s*constructible", re.I)
LAND_FEAT = re.compile(r"m² de terrain\s*([\s\d\u202f\u00a0,\.]+?)\s*m²", re.I)


def norm(s: str) -> float | None:
    t = (s.replace("\u202f", "").replace("\u00a0", "")
          .replace(" ", "").replace(",", "."))
    try:
        return float(t)
    except ValueError:
        return None


def main() -> int:
    shutil.copyfile(SRC, TMP)
    con = sqlite3.connect(TMP)
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT url, title, description, surface_m2, features
        FROM listings WHERE status='active'
    """).fetchall()
    drop_urls, terrain_urls, landsurf_urls = [], [], []
    for r in rows:
        blob = " ".join([r["title"] or "", r["description"] or "", r["features"] or ""])
        if TERRAIN_AD.search(blob):
            drop_urls.append(r["url"])
            terrain_urls.append(r["url"])
            continue
        m = LAND_FEAT.search(r["features"] or "")
        if m:
            tval = norm(m.group(1))
            if tval is not None and abs(tval - (r["surface_m2"] or -1)) < 1.0:
                drop_urls.append(r["url"])
                landsurf_urls.append(r["url"])
    con.executemany(
        "UPDATE listings SET status='excluded_land_surface' WHERE url=?",
        [(u,) for u in drop_urls])
    con.commit()
    con.close()
    print(f"excluded {len(drop_urls)} rows "
          f"({len(terrain_urls)} land adverts, {len(landsurf_urls)} land-surface rows)")

    cmd = [str(HERE / ".venv/bin/python"), str(HERE / "category_brief.py"),
           "--db", str(TMP), "--top", "10"]
    res = subprocess.run(cmd, capture_output=True, text=True, cwd=str(HERE))
    if res.returncode != 0:
        print(res.stdout)
        print(res.stderr, file=sys.stderr)
        return res.returncode
    OUT.write_text(res.stdout, encoding="utf-8")
    print(f"wrote {OUT} ({len(res.stdout)} chars)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
