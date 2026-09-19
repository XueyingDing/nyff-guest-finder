# Step 2 - find the famous guests.
# Reads nyff64_films.csv (from step 1), looks every guest up on Wikidata and judges how famous
# each one is from the number of Wikipedia language editions. Writes nyff64_films.xlsx with the
# Guests column colour-coded (very famous = green bold, famous = blue) plus their best-known works.
#
# Setup: pip install requests openpyxl
import csv
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from openpyxl import Workbook
from openpyxl.cell.rich_text import CellRichText, TextBlock
from openpyxl.cell.text import InlineFont
from openpyxl.styles import Alignment, Font

IN_FILE = "nyff64_films.csv"
OUT_FILE = "nyff64_films.xlsx"
CACHE_FILE = "wikidata_cache_v2.json"   # Lookup cache: reruns skip the network for people already looked up; safe to delete anytime

WORKERS = 5               # People looked up in parallel; too many risks Wikidata rate limiting (429). 3-6 recommended
ONLY_ACTORS = True        # True: count actors only; False: also count directors, writers and other film people
VERY_FAMOUS_MIN = 40      # Wikipedia language editions >= 40 -> very famous
FAMOUS_MIN = 15           # 15 ~ 39 → famous
WORKS_VERY_FAMOUS = 3     # Number of notable works listed for very famous
WORKS_FAMOUS = 1          # Number of notable works listed for famous
COLOR_VERY_FAMOUS = "FF008000"   # green (bold)
COLOR_FAMOUS = "FF0070C0"        # blue (not bold)
COLUMN_TITLES = {   # Special column titles; all other columns are auto-converted, e.g. "raw_details" -> "Raw Details"
    "very_famous_actors_works": "Very Famous Actors & Work",
    "famous_actors_works": "Famous Actors & Work",
}

API = "https://www.wikidata.org/w/api.php"
SPARQL = "https://query.wikidata.org/sparql"
HEADERS = {"User-Agent": "nyff-guest-lookup/1.0 (personal use; python-requests)"}

ACTOR_OCC = {"Q33999", "Q10800557"}  # actor, film actor
# Film-related occupations: film director, actor, film actor, screenwriter, film producer, cinematographer
FILM_OCC = ACTOR_OCC | {"Q2526255", "Q28389", "Q3282637", "Q222344"}
NOT_LANG_WIKI = {"commonswiki", "specieswiki", "metawiki", "wikidatawiki", "mediawikiwiki"}


def get_json(url, params, retries=3):
    for k in range(retries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=40)
            if r.status_code == 429:  # Rate limited: wait a bit and retry
                time.sleep(5 * (k + 1))
                continue
            r.raise_for_status()
            return r.json()
        except Exception:
            if k == retries - 1:
                raise
            time.sleep(2)
    return {}


def claim_ids(entity, pid):
    out = set()
    for c in entity.get("claims", {}).get(pid, []):
        v = c.get("mainsnak", {}).get("datavalue", {}).get("value")
        if isinstance(v, dict) and "id" in v:
            out.add(v["id"])
    return out


def count_wikis(entity):
    return sum(
        1 for k in entity.get("sitelinks", {})
        if k.endswith("wiki") and k not in NOT_LANG_WIKI
    )


def top_works(qid, limit=3):
    """Films this person directed or appeared in with the most Wikipedia language editions (English titles)"""
    q = f"""
    SELECT ?title ?sl WHERE {{
      {{ ?film wdt:P57 wd:{qid} . }} UNION {{ ?film wdt:P161 wd:{qid} . }}
      ?film wikibase:sitelinks ?sl .
      ?film rdfs:label ?title . FILTER(LANG(?title) = "en")
    }} ORDER BY DESC(?sl) LIMIT {limit}
    """
    data = get_json(SPARQL, {"query": q, "format": "json"})
    return [b["title"]["value"] for b in data.get("results", {}).get("bindings", [])]


def lookup_person(name):
    res = get_json(API, dict(
        action="wbsearchentities", search=name, language="en", uselang="en",
        type="item", limit=10, format="json"))
    ids = [x["id"] for x in res.get("search", [])]
    if not ids:
        return {"guest": name, "note": "not found on Wikidata"}

    ents = get_json(API, dict(
        action="wbgetentities", ids="|".join(ids),
        props="labels|claims|sitelinks", languages="en", format="json")).get("entities", {})

    cands = []
    for qid in ids:
        e = ents.get(qid)
        if not e or "Q5" not in claim_ids(e, "P31"):  # Humans only
            continue
        occ = claim_ids(e, "P106")
        en_label = e.get("labels", {}).get("en", {}).get("value", "")
        cands.append({
            "qid": qid,
            "exact": en_label.lower() == name.lower(),
            "film": bool(occ & FILM_OCC),
            "is_actor": bool(occ & ACTOR_OCC),
            "n": count_wikis(e),
        })
    # Require a film-related occupation so a same-name non-film person isn't mistaken for a star
    cands = [c for c in cands if c["film"]]
    if not cands:
        return {"guest": name, "note": "no matching film person"}

    cands.sort(key=lambda c: (c["exact"], c["n"]), reverse=True)
    best = cands[0]

    notes = []
    if not best["exact"]:
        notes.append("name not an exact match")
    same = [c for c in cands[1:] if c["exact"] and c["n"] >= 0.5 * max(best["n"], 1)]
    if same:
        notes.append("possible same-name film person")

    works = []
    if best["n"] >= FAMOUS_MIN and (best["is_actor"] or not ONLY_ACTORS):
        try:
            works = top_works(best["qid"], max(WORKS_VERY_FAMOUS, WORKS_FAMOUS))
        except Exception as ex:
            notes.append(f"works lookup failed: {str(ex)[:60]}")

    return {
        "guest": name,
        "qid": best["qid"],
        "is_actor": best["is_actor"],
        "wiki_langs": best["n"],
        "works": works,
        "note": "; ".join(notes),
    }


def guest_tier(g, cache):
    """Return 'very' / 'famous' / None"""
    r = cache.get(g, {})
    if not r.get("qid"):
        return None
    if ONLY_ACTORS and not r.get("is_actor"):
        return None
    n = r.get("wiki_langs", 0)
    if n >= VERY_FAMOUS_MIN:
        return "very"
    if n >= FAMOUS_MIN:
        return "famous"
    return None


def build_works(guests, cache):
    vf, f = [], []
    for g in guests:
        tier = guest_tier(g, cache)
        if not tier:
            continue
        works = cache[g].get("works", [])
        if tier == "very":
            vf.append(f"{g}: {', '.join(works[:WORKS_VERY_FAMOUS]) or '(none found)'}")
        else:
            f.append(f"{g}: {', '.join(works[:WORKS_FAMOUS]) or '(none found)'}")
    return {
        "very_famous_actors_works": "\n".join(vf),   # One actor per line
        "famous_actors_works": "\n".join(f),
    }


def rich_guests(guests, cache):
    """Guests column: very famous = green bold, famous = blue not bold, everyone else = plain black"""
    base = dict(rFont="Calibri", sz=11)
    if not any(guest_tier(g, cache) for g in guests):
        return "; ".join(guests)
    parts = []
    for i, g in enumerate(guests):
        if i:
            parts.append(TextBlock(InlineFont(**base), "; "))
        tier = guest_tier(g, cache)
        if tier == "very":
            font = InlineFont(b=True, color=COLOR_VERY_FAMOUS, **base)
        elif tier == "famous":
            font = InlineFont(color=COLOR_FAMOUS, **base)
        else:
            font = InlineFont(**base)
        parts.append(TextBlock(font, g))
    return CellRichText(*parts)


_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def clean(v):
    return _ILLEGAL.sub("", v) if isinstance(v, str) else v


def main():
    with open(IN_FILE, encoding="utf-8-sig", newline="") as f:
        # Lowercase the headers so both "Title" and "title" work
        films = [{(k or "").strip().lower(): v for k, v in r.items()}
                 for r in csv.DictReader(f)]

    all_guests = []
    for row in films:
        for g in [x.strip() for x in row.get("guests", "").split(";") if x.strip()]:
            if g not in all_guests:
                all_guests.append(g)
    print(f"{len(all_guests)} distinct guests")

    cache = {}
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, encoding="utf-8") as f:
            cache = json.load(f)

    todo = [g for g in all_guests if g not in cache]
    print(f"{len(all_guests) - len(todo)} already cached; {len(todo)} to look up online")

    def safe_lookup(name):
        try:
            return name, lookup_person(name)
        except Exception as ex:
            return name, {"guest": name, "note": f"lookup error: {str(ex)[:80]}"}

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(safe_lookup, g) for g in todo]
        for done, fut in enumerate(as_completed(futures), 1):
            name, r = fut.result()
            cache[name] = r
            print(f"[{done}/{len(todo)}] {name} -> langs={r.get('wiki_langs', '-')} "
                  f"actor={r.get('is_actor', '-')} {r.get('note', '')}")
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=1)
    print(f"Lookups took {time.time() - t0:.0f} s")

    work_cols = ["very_famous_actors_works", "famous_actors_works"]
    tail = ["sessions"]  # Goes in the last column
    head = [c for c in films[0].keys() if c not in tail and c != "raw_details"]
    fields = head + work_cols + tail

    wb = Workbook()
    ws = wb.active
    ws.title = "films"
    ws.append([COLUMN_TITLES.get(c, c.replace("_", " ").title()) for c in fields])
    for idx, cell in enumerate(ws[1]):
        col = fields[idx]
        if col == "very_famous_actors_works":
            cell.font = Font(bold=True, color=COLOR_VERY_FAMOUS)
        elif col == "famous_actors_works":
            cell.font = Font(bold=True, color=COLOR_FAMOUS)
        else:
            cell.font = Font(bold=True)

    for row in films:
        guests = [x.strip() for x in row.get("guests", "").split(";") if x.strip()]
        row.update(build_works(guests, cache))
        values = []
        for col in fields:
            if col == "guests":
                values.append(rich_guests(guests, cache))
            elif col == "sessions":
                # One session per line (line breaks inside the cell)
                values.append(clean(row.get(col, "")).replace(" | ", "\n"))
            else:
                values.append(clean(row.get(col, "")))
        ws.append(values)

    # Layout: top alignment, text wrapping, frozen header row, sensible column widths
    widths = {"title": 28, "director": 22, "guests": 34,
              "very_famous_actors_works": 46, "famous_actors_works": 34,
              "sessions": 40, "raw_details": 70}
    for idx, col in enumerate(fields, 1):
        letter = ws.cell(row=1, column=idx).column_letter
        ws.column_dimensions[letter].width = widths.get(col, 20)
    for r in ws.iter_rows(min_row=2):
        for cell in r:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            col = fields[cell.column - 1]
            if col == "very_famous_actors_works":
                cell.font = Font(bold=True, color=COLOR_VERY_FAMOUS)   # green bold
            elif col == "famous_actors_works":
                cell.font = Font(color=COLOR_FAMOUS)                    # blue, not bold
    ws.freeze_panes = "A2"
    wb.save(OUT_FILE)

    print(f"\nSaved: {os.path.abspath(OUT_FILE)}")


if __name__ == "__main__":
    main()
