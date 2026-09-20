# NYFF64 Guest Finder

Finds which screenings at the **64th New York Film Festival** have an intro or Q&A, who is attending, and which of those guests are well-known actors. The result is a colour-coded Excel sheet, optionally with Chinese names and film titles.

> A personal, non-commercial project. Not affiliated with Film at Lincoln Center.

## Why

On the [lineup page](https://www.filmlinc.org/nyff/nyff64-lineup/?tab=films), the guests for a screening only appear in a tooltip when you hover over the showtime. Checking every film by hand and googling each name is slow, so this automates it.

## Pipeline

```
Lineup page ──► 01_scrape_screenings.py ──► nyff64_films.csv
                                                  │
                        02_lookup_famous_guests.py ▼
                                            nyff64_films.xlsx
                                                  │
                          03_add_chinese_names.py ▼
                                 nyff64_films_with_chinese.xlsx
```

## Quick start

Requires Python 3.9+.

```bash
git clone https://github.com/XueyingDing/nyff-guest-finder.git
cd nyff-guest-finder

pip install -r requirements.txt
playwright install chromium

python scripts/01_scrape_screenings.py
python scripts/02_lookup_famous_guests.py
python scripts/03_add_chinese_names.py     # optional
```

Run the scripts from the repository root; each step reads the file the previous step wrote.

## Steps

### 1. Scrape the lineup (`01_scrape_screenings.py`)

Output: `nyff64_films.csv` (Title, Director, Guests, Sessions)

- **Playwright** opens the page in a real browser and finds each film card from its `/films/` link.
- Hovers over every **Intro** / **Q&A** showtime button and reads the tooltip.
- A **regular expression** pulls the guest names out of the tooltip text and drops job titles (e.g. "restoration coordinator").
- Films without an Intro or Q&A are dropped.

### 2. Find the famous guests (`02_lookup_famous_guests.py`)

Output: `nyff64_films.xlsx`

- **Wikidata search** matches each name to a person with a film-related occupation.
- Fame is estimated from the number of **Wikipedia language editions**: 40 or more is "very famous", 15 to 39 is "famous". Only actors are counted by default.
- A **SPARQL query** returns each person's best-known films.
- **openpyxl** writes the Excel file: very famous guests in green bold, famous guests in blue.
- Lookups run in parallel and are cached, so reruns are fast.

### 3. Add Chinese names (`03_add_chinese_names.py`, optional)

Output: `nyff64_films_with_chinese.xlsx`

- Reads Chinese labels from **Wikidata** for the actors and their films.
- Film titles are matched by English title within that actor's own filmography.
- Anything without a Chinese label keeps its English name.
- Inserts a Chinese column next to each English one, keeping the formatting.

## Retries, failures & cache recovery

Steps 2 and 3 call the Wikidata API in parallel (`WORKERS`). A single lookup can fail
outright — network error, timeout, or Wikidata rate-limiting (HTTP 429) — as opposed to
succeeding and genuinely finding no match, and the scripts tell the two apart:

- A **failed** lookup is never cached, and is retried automatically the next time you
  run the same script — just run it again.
- A **confirmed negative** (the request succeeded; no matching person/name/title was
  found) is cached and won't be queried again.
- In `nyff64_films.xlsx`, a guest whose lookup failed this run shows up in orange
  italics in the Guests column and is listed in the "Lookup Pending (retry next run)"
  column — this is different from a guest who was found and determined not to be
  famous (plain black text). Re-running the script clears them once they resolve.
- Each retry/wait is logged (what was being looked up, attempt number, reason, wait
  time) so a slow or rate-limited run is visible in the log instead of looking stalled.
  Set `VERBOSE_RETRIES = False` at the top of either script to silence these lines.

If you have a `wikidata_cache_v2.json` or `zh_lookup_cache.json` from before this
retry/caching behaviour existed, sustained rate limiting could have made the scripts
cache a failed request as if it had succeeded and found nothing. Recover it with:

```bash
python scripts/recover_cache.py --dry-run   # preview what would change
python scripts/recover_cache.py             # back up, then clean up
```

This backs up each cache file (`<file>.bak-<timestamp>`) and only drops entries that
look like they could have been affected — you never need to delete a whole cache file;
the next run of `02_lookup_famous_guests.py` / `03_add_chinese_names.py` re-queries
just what was dropped. Add `--purge-unmatched-works` to also re-verify "title not found
in this actor's filmography" results (off by default — most of those are correct, not
bug artifacts).

## Running the tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

The suite (25 tests, offline — every Wikidata request is mocked, nothing under
`results/` or the repo root is touched) covers the retry/caching behaviour above:
persistent rate-limiting must raise instead of being cached as a false negative, a
failed lookup must be retried next run, a successful one must not be re-queried, and an
actor whose identity is known but whose Chinese label/works lookup failed must not lose
that identity in the process.

## Results

The `results/` folder holds the final spreadsheet from one run. It is only a snapshot: the lineup and guests may have changed since. The festival content belongs to Film at Lincoln Center and is shown here for demonstration.

## Configuration

Set at the top of the scripts:

| Setting | Default | Meaning |
|---|---|---|
| `ONLY_ACTORS` | `True` | Only mark actors; `False` also includes directors, writers, etc. |
| `VERY_FAMOUS_MIN` / `FAMOUS_MIN` | `40` / `15` | Wikipedia editions needed for each level |
| `WORKS_VERY_FAMOUS` / `WORKS_FAMOUS` | `3` / `1` | Number of best-known works listed |
| `WORKERS` | `3` | Parallel lookups; too many can trigger rate limiting |
| `VERBOSE_RETRIES` | `True` | Log each retry/wait (see [Retries, failures & cache recovery](#retries-failures--cache-recovery)) |

## Limitations

- The fame score is a rough heuristic based on international Wikipedia coverage.
- Wikidata coverage is uneven: some guests are missing, and newer films often have no Chinese title.
- Same-name people can be matched wrongly; check anything the console flags.
- Step 1 depends on the site's current page layout and may break if it changes.

## Responsible use

For personal, non-commercial use. Check the site's terms of use before running the scraper, and do not republish the scraped festival content. Wikidata's data is released under CC0.
