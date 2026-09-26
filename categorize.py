#!/usr/bin/env python3
"""Classify each listing by WHAT YOU'D DO WITH IT, not just what it costs.

Why this exists: a ranked €/m² table answered "which is cheapest" and nothing
else. The same €71/m² can be a 12th-century shell with no water, a warehouse with
no residential consent, or a brick bâtisse needing only paint — and the user's
question ("are these rentals, renovations, or places to live?") has no answer in
the price. Every one of the signals below was already in the scraped data.

Four lanes (the user is interested in all of them):

  yield       rental play — let-ability, multi-unit potential, commercial income
  renovation  works project — needs money, time and probably a builder
  livein      habitable home — move in, or light cosmetic work
  commercial  not housing — entrepôt / hangar / atelier / bureaux (own exit risk)

Classification is description-first. French portals describe condition in plain
words far more reliably than they populate structured fields: only 21% of rows
carry a DPE, but 36% say "à rénover"/"travaux"/"potentiel" in prose. Where a DPE
does exist it is treated as strong confirmation (F/G is a legal energy-renovation
trigger, and it is the one signal a vendor cannot spin).

Scoring = weighted keyword evidence + structural signals, and the LANE WITH THE
MOST EVIDENCE WINS. Deliberately not a priority cascade: "immeuble 4 appartements
à rénover" is genuinely BOTH a yield play and a works project, so it records a
primary lane plus secondary flags rather than being forced into one bucket.
"""
from __future__ import annotations

import re
import sqlite3
import sys
from collections import Counter

# --- evidence patterns -------------------------------------------------------
# Weight 3 = unambiguous; 2 = strong; 1 = supporting context.
YIELD = [
    (3, r"\b(?:immeuble de rapport|rapport\b|rendement\b)"),
    (3, r"\b(?:revenus?|déjà loué|actuellement loué|loué\b)"),
    (3, r"\binvestisseur|\binvestissement locatif|\blocatif\b"),
    (3, r"\b\d+\s*appartements?\b"),          # "4 appartements" = real multi-unit
    (2, r"\bappartements?\b"),
    (2, r"\b(?:local commercial|boutique|commerce)\b"),
    (2, r"\b(?:colocation|chambres? à louer|bail\b)"),
    (2, r"\b(?:diviser|division|découper)\b.{0,25}\b(?:appartements?|logements?|lots?)\b"),
    (1, r"\b(?:centre[- ]ville|gare|quartier)\b.{0,40}\b(?:immeuble|rapport|locatif)\b"),
    (1, r"\b(?:immeuble|ensemble)\b"),
]
RENOVATION = [
    (3, r"\bà rénover\b|\ba renover\b|\bà restaurer\b|\bà réhabiliter\b|\bà rehabiliter\b"),
    (3, r"\bnon raccordé|\bnon équip\w*|\bsans (?:eau|électricité|electricite|assainissement|chauffage)\b"),
    (3, r"\bà démolir\b|\ben ruine\b|\bà reconstruire\b|\bsans toiture\b|effondr"),
    (2, r"\btravaux\b"),
    (2, r"\bà aménager\b|\ba amenager\b|\bà finir\b|\bà terminer\b|\bà moderniser\b"),
    (2, r"\bfort potentiel\b|\bpotentiel d['’]aménagement\b|\bpotentiel de rénovation\b"),
    (2, r"\bdans son jus\b|\bà rafraîchir\b|\ba rafraichir\b|\bà remettre au goût\b"),
    (1, r"\bancien(?:ne)?\b"),
    (1, r"\bà usage d['’]habitation\b.{0,60}\btravaux\b"),
]
LIVEIN = [
    (3, r"\bsans travaux\b|\bprêt à vivre\b|\bpret a vivre\b|\bhabitable immédiatement\b"),
    (3, r"\b(?:entièrement|complètement|récemment) rénové"),
    (2, r"\brénové|\brenove|\brefait\b|\bremis à neuf\b|\bde 20\d\d\b"),
    (2, r"\ben parfait état\b|\btrès bon état\b|\bbon état\b|\bimpeccable\b"),
    (2, r"\bhabitable\b"),
    (1, r"\bfamilial|\bplain[- ]pied\b|\bcoup de cœur\b|\bimmédiatement\b"),
    (1, r"\bjardin\b|\bterrasse\b|\bcave\b|\bgarage\b"),
]
COMMERCIAL = [
    (3, r"\bentrep[oô]t\b|\bhangar\b|\batelier\b|\bcuvage\b"),
    (3, r"\b(?:local|locaux) (?:commercial|professionnel|d['’]activité)"),
    (2, r"\bbureaux?\b"),
    (2, r"\bparking\b|\bgarage(s)? .{0,20}(?:collectif|voitures)\b"),
    (2, r"\bactivité\b|\bsociété\b|\bprofession libérale\b|\bgaragiste\b"),
]

LANE_PATTERNS = {"yield": YIELD, "renovation": RENOVATION,
                 "livein": LIVEIN, "commercial": COMMERCIAL}

# A DPE of F/G makes energy renovation a certainty in law, so it outranks prose.
DPE_RENOVATION = {"F", "G"}
DPE_LIVEIN = {"A", "B", "C", "D", "E"}
# Above this surface without any multi-unit or yield signal, a "maison" priced in
# the entry band is far more often a project than a home.
BIG_SURFACE_RENO = 300


def classify(title: str | None, description: str | None, dpe: str | None = None,
             surface: float | None = None) -> dict:
    """Return {category, confidence, flags, scores} for one listing."""
    text = f"{title or ''} \n {description or ''}".lower()
    scores = Counter()
    why = {}
    for lane, pats in LANE_PATTERNS.items():
        for weight, pat in pats:
            m = re.search(pat, text)
            if m:
                scores[lane] += weight
                why.setdefault(lane, []).append(m.group(0).strip()[:38])

    d = (dpe or "").strip().upper()
    if d in DPE_RENOVATION:
        scores["renovation"] += 4
        why.setdefault("renovation", []).append(f"DPE {d}")
    elif d in DPE_LIVEIN:
        scores["livein"] += 2
        why.setdefault("livein", []).append(f"DPE {d}")

    # A large building in the entry price band with no yield/commercial story is
    # usually a project: 340 m² of shell and 340 m² of finished home are the same
    # number on the card and opposite purchases.
    if surface and surface >= BIG_SURFACE_RENO and scores["yield"] < 3 \
            and scores["commercial"] < 3:
        scores["renovation"] += 1

    if not any(scores.values()):
        # No evidence either way — do NOT invent a lane. Surfaced separately so
        # the report can say "unknown" instead of guessing.
        return {"category": "unclassified", "confidence": "none",
                "flags": [], "scores": dict(scores), "why": {}}

    top = max(scores.values())
    winners = [k for k, v in scores.items() if v == top]
    # Ties: commercial (own exit risk) > yield (income) > renovation (works) >
    # livein. Ties are common for "immeuble à rénover", where both are true.
    category = winners[0]
    for pref in ("commercial", "yield", "renovation", "livein"):
        if pref in winners:
            category = pref
            break

    # MIXED-USE is the mandate, not an edge case: "immeuble de rapport composé d'un
    # local commercial et 3 appartements". The plain tie-break files that under
    # 'commercial' (own-exit-risk framing), when the asset's exit is really the
    # residential rent roll. Surfaced by the century21 enrichment, where this
    # mislabelled 50+ dept-02 buildings as purely commercial.
    _living = re.search(r"\bappartements?|\blogements?|\bhabitation\b|\bpi[eè]ces?\b", text)
    _commercial = re.search(r"\blocal commercial|\bcommerce\b|\bboutique\b|\bbureaux?\b", text)
    mixed_use = bool(_living and _commercial)
    if mixed_use and scores.get("yield"):
        category = "yield"

    flags = sorted(k for k, v in scores.items() if k != category and v >= 2)
    if mixed_use:
        flags = sorted(set(flags) | {"commercial", "mixed_use"})
    conf = "high" if top >= 6 else "medium" if top >= 3 else "low"
    return {"category": category, "confidence": conf, "flags": flags,
            "scores": dict(scores), "why": {k: v[:3] for k, v in why.items()}}


def ensure_schema(con: sqlite3.Connection) -> None:
    cols = [c[1] for c in con.execute("PRAGMA table_info(listings)")]
    for name, decl in (("category", "TEXT"), ("category_confidence", "TEXT"),
                       ("category_flags", "TEXT"), ("source_ref", "TEXT"),
                       ("miss_count", "INTEGER DEFAULT 0")):
        if name not in cols:
            con.execute(f"ALTER TABLE listings ADD COLUMN {name} {decl}")
    con.execute("CREATE INDEX IF NOT EXISTS idx_cat ON listings(category)")


def main() -> int:
    db = sys.argv[1] if len(sys.argv) > 1 else "listings.db"
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    ensure_schema(con)

    rows = con.execute("""SELECT url, title, description, dpe_energy, surface_m2, source
                          FROM listings WHERE status='active'""").fetchall()
    dist = Counter()
    for r in rows:
        res = classify(r["title"], r["description"], r["dpe_energy"], r["surface_m2"])
        dist[res["category"]] += 1
        con.execute("""UPDATE listings SET category=?, category_confidence=?,
                       category_flags=? WHERE url=?""",
                    (res["category"], res["confidence"],
                     ",".join(res["flags"]), r["url"]))
    con.commit()

    total = len(rows)
    print(f"classified {total} active listings")
    for cat, n in dist.most_common():
        print(f"  {cat:13s} {n:5d}  ({100.0*n/total:.0f}%)")

    print("\n=== lanes among the >=150 m2, EUR10k-220k criteria set ===")
    for r in con.execute("""SELECT COALESCE(category,'(null)') c, COUNT(*) n,
                                   ROUND(AVG(price_eur/surface_m2)) eur_m2,
                                   ROUND(AVG(price_eur)) avg_price
                            FROM listings
                            WHERE status='active' AND surface_m2 BETWEEN 150 AND 1500
                              AND price_eur BETWEEN 10000 AND 220000
                            GROUP BY 1 ORDER BY 2 DESC"""):
        print(f"  {r['c']:13s} {r['n']:5d} rows   avg €{r['avg_price']:,.0f}   "
              f"avg €{r['eur_m2']:,.0f}/m²")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
