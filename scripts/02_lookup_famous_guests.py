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
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from openpyxl import Workbook
from openpyxl.cell.rich_text import CellRichText, TextBlock
from openpyxl.cell.text import InlineFont
from openpyxl.styles import Alignment, Font

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _wikidata_common import (
    LookupUnavailable, claim_ids, count_wikis, get_json as _get_json, make_stdout_unicode_safe,
)

make_stdout_unicode_safe()

IN_FILE = "nyff64_films.csv"
OUT_FILE = "nyff64_films.xlsx"
CACHE_FILE = "wikidata_cache_v2.json"   # Lookup cache: reruns skip the network for people already looked up; safe to delete anytime

VERBOSE_RETRIES = True    # Print a line for every retry/wait (attempt count, reason, wait time),
                          # so a slow or throttled run is visible in the log instead of looking stalled.
WORKERS = 3               # People looked up in parallel; too many risks Wikidata rate limiting (429). 3-6 recommended
ONLY_ACTORS = True        # True: count actors only; False: also count directors, writers and other film people
VERY_FAMOUS_MIN = 40      # Wikipedia language editions >= 40 -> very famous
FAMOUS_MIN = 15           # 15 ~ 39 → famous
WORKS_VERY_FAMOUS = 3     # Number of notable works listed for very famous
WORKS_FAMOUS = 1          # Number of notable works listed for famous
COLOR_VERY_FAMOUS = "FF008000"   # green (bold)
COLOR_FAMOUS = "FF0070C0"        # blue (not bold)
COLOR_PENDING = "FFCC6600"       # orange (italic) - lookup failed this run, will retry; NOT the same as "not famous"
COLUMN_TITLES = {   # Special column titles; all other columns are auto-converted, e.g. "raw_details" -> "Raw Details"
    "very_famous_actors_works": "Very Famous Actors & Work",
    "famous_actors_works": "Famous Actors & Work",
    "lookup_pending": "Lookup Pending (retry next run)",
}

API = "https://www.wikidata.org/w/api.php"
SPARQL = "https://query.wikidata.org/sparql"
HEADERS = {"User-Agent": "nyff-guest-lookup/1.0 (personal use; python-requests)"}


def get_json(url, params, retries=3):
    return _get_json(url, params, HEADERS, retries=retries, verbose=VERBOSE_RETRIES)


ACTOR_OCC = {"Q33999", "Q10800557"}  # actor, film actor
# Film-related occupations: film director, actor, film actor, screenwriter, film producer, cinematographer
FILM_OCC = ACTOR_OCC | {"Q2526255", "Q28389", "Q3282637", "Q222344"}


def top_works(qid, limit=3):
    """Films this person directed or appeared in with the most Wikipedia language editions (English titles).
    May raise LookupUnavailable if the SPARQL request fails -- callers must decide what that means."""
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
    """Resolve a guest's name to a Wikidata person.

    Returns a dict with a "status" field:
      - "ok":        person identified; qid/is_actor/wiki_langs are set.
                      "works_incomplete" is True if we couldn't fetch their notable
                      works this run (separate transient failure) -- retry that part later.
      - "not_found": the request(s) succeeded but no matching film person was found.
                      This is a real, cacheable negative.

    Raises LookupUnavailable if the Wikidata request(s) themselves failed (network,
    timeout, persistent 429) -- the caller must NOT cache this as a result.
    """
    res = get_json(API, dict(
        action="wbsearchentities", search=name, language="en", uselang="en",
        type="item", limit=10, format="json"))
    ids = [x["id"] for x in res.get("search", [])]
    if not ids:
        return {"guest": name, "status": "not_found", "note": "not found on Wikidata"}

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
        return {"guest": name, "status": "not_found", "note": "no matching film person"}

    cands.sort(key=lambda c: (c["exact"], c["n"]), reverse=True)
    best = cands[0]

    notes = []
    if not best["exact"]:
        notes.append("name not an exact match")
    same = [c for c in cands[1:] if c["exact"] and c["n"] >= 0.5 * max(best["n"], 1)]
    if same:
        notes.append("possible same-name film person")

    works = []
    works_incomplete = False
    if best["n"] >= FAMOUS_MIN and (best["is_actor"] or not ONLY_ACTORS):
        try:
            works = top_works(best["qid"], max(WORKS_VERY_FAMOUS, WORKS_FAMOUS))
        except LookupUnavailable as ex:
            notes.append(f"works lookup failed: {str(ex)[:60]}")
            works_incomplete = True

    return {
        "guest": name,
        "status": "ok",
        "qid": best["qid"],
        "is_actor": best["is_actor"],
        "wiki_langs": best["n"],
        "works": works,
        "works_incomplete": works_incomplete,
        "note": "; ".join(notes),
    }


def guest_tier(g, cache):
    """Return 'very' / 'famous' / 'pending' / None.
    'pending' means the lookup for this guest failed this run (network/rate limit) and
    will be retried next run -- their fame is unknown, NOT confirmed absent, so they must
    not be rendered the same way as a guest we successfully identified as not famous."""
    r = cache.get(g, {})
    if r.get("status") == "error":
        return "pending"
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
    vf, f, pending = [], [], []
    for g in guests:
        tier = guest_tier(g, cache)
        if tier == "pending":
            pending.append(g)
            continue
        if not tier:
            continue
        r = cache.get(g, {})
        works = r.get("works", [])
        limit = WORKS_VERY_FAMOUS if tier == "very" else WORKS_FAMOUS
        if works:
            text = ", ".join(works[:limit])
        elif r.get("works_incomplete"):
            text = "(works lookup incomplete, will retry next run)"
        else:
            text = "(none found)"
        (vf if tier == "very" else f).append(f"{g}: {text}")
    return {
        "very_famous_actors_works": "\n".join(vf),   # One actor per line
        "famous_actors_works": "\n".join(f),
        "lookup_pending": "; ".join(pending),
    }


def rich_guests(guests, cache):
    """Guests column: very famous = green bold, famous = blue not bold,
    pending (lookup failed, will retry) = orange italic, everyone else = plain black"""
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
        elif tier == "pending":
            font = InlineFont(i=True, color=COLOR_PENDING, **base)
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

    if not films:
        sys.exit(
            f"No film rows found in {os.path.abspath(IN_FILE)} -- nothing to look up.\n"
            f"Run 01_scrape_screenings.py first, or check that the CSV has data rows "
            f"below the header."
        )

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

    # Retry anyone never looked up, anyone whose last lookup failed (network/429), and
    # anyone whose person was identified but whose notable-works lookup didn't finish.
    todo = [g for g in all_guests
            if g not in cache
            or cache[g].get("status") == "error"
            or cache[g].get("works_incomplete")]
    print(f"{len(all_guests) - len(todo)} already cached; {len(todo)} to look up online", flush=True)

    def safe_lookup(name):
        print(f"  [start] {name}", flush=True)
        try:
            return name, lookup_person(name)
        except LookupUnavailable as ex:
            return name, {"guest": name, "status": "error", "note": f"lookup unavailable: {str(ex)[:120]}"}
        except Exception as ex:
            return name, {"guest": name, "status": "error", "note": f"lookup error: {str(ex)[:120]}"}

    t0 = time.time()
    failed_this_run = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(safe_lookup, g) for g in todo]
        for done, fut in enumerate(as_completed(futures), 1):
            try:
                name, r = fut.result()
                if r.get("status") == "error":
                    # Do NOT cache a failed request -- keep whatever (possibly better) entry
                    # was already there, if any, and retry this guest again next run.
                    failed_this_run.append(name)
                    print(f"[{done}/{len(todo)}] {name} -> FAILED, will retry next run ({r.get('note', '')})",
                          flush=True)
                    continue
                cache[name] = r
                print(f"[{done}/{len(todo)}] {name} -> langs={r.get('wiki_langs', '-')} "
                      f"actor={r.get('is_actor', '-')} {r.get('note', '')}", flush=True)
                with open(CACHE_FILE, "w", encoding="utf-8") as f:
                    json.dump(cache, f, ensure_ascii=False, indent=1)
            except Exception as ex:
                # Never let one guest's unexpected failure (console encoding, a disk
                # hiccup, ...) abort the whole batch and strand every other in-flight
                # lookup with nothing saved -- see make_stdout_unicode_safe()'s docstring
                # for exactly this failure mode as it played out in practice.
                print(f"[{done}/{len(todo)}] unexpected error recording a result: {ex!r}", flush=True)
    print(f"Lookups took {time.time() - t0:.0f} s", flush=True)

    ok = [g for g in all_guests if cache.get(g, {}).get("status") == "ok"]
    not_found = [g for g in all_guests if cache.get(g, {}).get("status") == "not_found"]
    never_resolved = [g for g in all_guests if g not in cache]
    print(f"\nLookup summary: {len(ok)} resolved, {len(not_found)} not found on Wikidata, "
          f"{len(failed_this_run)} failed this run (will retry next run)")
    if failed_this_run:
        print("  Failed this run: " + ", ".join(failed_this_run))
    if never_resolved:
        print("  Never resolved (no cache entry yet): " + ", ".join(never_resolved))
    if not_found:
        print("  Not found / no matching film person: " + ", ".join(not_found))

    work_cols = ["very_famous_actors_works", "famous_actors_works", "lookup_pending"]
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
        elif col == "lookup_pending":
            cell.font = Font(bold=True, color=COLOR_PENDING)
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
              "lookup_pending": 30, "sessions": 40, "raw_details": 70}
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
            elif col == "lookup_pending":
                cell.font = Font(italic=True, color=COLOR_PENDING)      # orange, not bold
    ws.freeze_panes = "A2"
    wb.save(OUT_FILE)

    print(f"\nSaved: {os.path.abspath(OUT_FILE)}")


if __name__ == "__main__":
    main()
