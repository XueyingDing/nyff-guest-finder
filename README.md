# NYFF64 Guest Finder

Find out which screenings at the **64th New York Film Festival (NYFF64)** come with an introduction or a Q&A, and which of the guests attending are well-known actors, so you can pick screenings with a bit of extra star power. The result is a colour-coded Excel sheet, optionally with Chinese names and film titles.

> A personal, non-commercial project. Not affiliated with Film at Lincoln Center.

## Why this exists

The [NYFF64 lineup page](https://www.filmlinc.org/nyff/nyff64-lineup/?tab=films) lists every film and showtime. Some showtimes are tagged **Intro** or **Q&A**, but *who* is actually attending is only shown in a tooltip when you hover over the showtime button. With around 87 films on the page (at the time I ran this), checking each one by hand, then googling every unfamiliar name, is slow.

This project automates it in three small steps:

1. **Scrape** the lineup and collect the guests for every Intro / Q&A screening.
2. **Judge** which guests are famous actors and list their best-known films.
3. **Translate** actor names and film titles into Chinese (optional).

## Pipeline

```
NYFF lineup page
      │
      ▼
01_scrape_screenings.py ──► nyff64_films.csv
                                   │
                                   ▼   (looks guests up on Wikidata)
                   02_lookup_famous_guests.py ──► nyff64_films.xlsx
                                                        │
                                                        ▼   (looks up Chinese labels on Wikidata)
                                        03_add_chinese_names.py ──► nyff64_films_with_chinese.xlsx
```

## Example output

Illustrative layout only; the real values are looked up on Wikidata when you run the scripts.

| Title | Director | Guests | Very Famous Actors & Work | Sessions |
|---|---|---|---|---|
| PAPER TIGER | James Gray | James Gray; **Adam Driver**; **Scarlett Johansson**; Miles Teller | Adam Driver: Marriage Story, … <br> Scarlett Johansson: Her, … | 6:00 PM Q&A: … <br> 6:30 PM INTRO: … |

In the real spreadsheet, very famous guests are **green and bold**, famous guests are blue, everyone else is plain black. Sessions and actors each get their own line inside the cell.

## Quick start

Requires Python 3.9+.

```bash
git clone https://github.com/<your-username>/nyff64-guest-finder.git
cd nyff64-guest-finder

python -m venv .venv
# Windows:  .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate

pip install -r requirements.txt
playwright install chromium

python scripts/01_scrape_screenings.py
python scripts/02_lookup_famous_guests.py
python scripts/03_add_chinese_names.py     # optional
```

Run the scripts from the repository root. Each one reads and writes files in the current folder, so the outputs of one step are automatically found by the next.

## What each step does

### Step 1 - `01_scrape_screenings.py`

**Input:** the live lineup page. **Output:** `nyff64_films.csv` with the columns `Title`, `Director`, `Guests`, `Sessions`.

- Opens the page in a visible Chromium window using [Playwright](https://playwright.dev/python/), because the guest names live in tooltips that only appear on hover.
- Scrolls to the bottom so lazily loaded films are all present.
- **Finds the film cards structurally, not by position.** It collects every link that points to `/films/<slug>`, then climbs up the DOM until the container would include a second film. That container is the card. (An earlier version used long `nth-child` selectors copied from the browser dev tools; they silently skipped the first film.)
- For each card it reads the title and director, then looks at the showtime buttons. Only buttons whose label contains **Intro** or **Q&A** matter; films without any are dropped.
- **Hovers each of those buttons and reads the tooltip.** The button is scrolled to the middle of the screen (so a sticky header cannot cover it), the mouse is moved onto it by coordinates, and the tooltip text is read. Hovering occasionally fails to open the tooltip, so it retries up to three times.
- **Extracts the guest names** from text such as `Q&A w. Fred Camper and restoration coordinator Kyle Westphal • Standby tickets may be available…`: it takes what follows `w.` / `with` / `by` before the first `•`, splits on commas / "and" / "&", and keeps the last run of capitalised words in each piece. That drops lowercase job titles, giving `Fred Camper; Kyle Westphal`.
- Keeps a per-session record such as `6:30 PM INTRO: James Gray`, so a film with two intros by different guests still fits on one row, with every guest listed and each session's guests preserved.
- Saves whatever it has collected even if the run errors out or is interrupted with Ctrl+C.

### Step 2 - `02_lookup_famous_guests.py`

**Input:** `nyff64_films.csv`. **Output:** `nyff64_films.xlsx`.

- Collects the distinct guest names and looks each one up on [Wikidata](https://www.wikidata.org/) (search, then fetch details).
- Keeps only candidates that are **humans with a film-related occupation** (actor, director, screenwriter, producer, cinematographer), preferring an exact name match and then the candidate with the most Wikipedia editions. This avoids matching a same-named non-film person.
- **Fame proxy:** the number of Wikipedia *language editions* the person has. There is no official measure of fame, so this is a rough heuristic:
  - 40 or more: **very famous**
  - 15 to 39: **famous**
  - fewer: not marked
- By default only **actors** are marked (directors, critics and restorers are ignored); this is configurable.
- **Best-known works:** a SPARQL query returns the films the person directed or appeared in, sorted by number of Wikipedia editions. Very famous actors get their top 3, famous actors their top 1.
- Lookups run 5 at a time and are cached in `wikidata_cache_v2.json`, so reruns are fast and you can safely interrupt and resume.
- Writes an Excel file (CSV cannot store colours) with the `Guests` column colour-coded, works listed one actor per line, and sessions one per line.

### Step 3 - `03_add_chinese_names.py` (optional)

**Input:** `nyff64_films.xlsx`. **Output:** `nyff64_films_with_chinese.xlsx`.

- Reads the two actor/work columns and inserts a Chinese version right after each one, copying the colours, wrapping and column widths. Your original file is left untouched.
- Actor names: the Chinese label of the same Wikidata person that step 2 identified (it reuses step 2's cache, so it cannot switch to a different person with the same name).
- Film titles: matched by English title **within that person's own filmography**, so a same-titled film by someone else is never picked.
- Some titles contain a comma (for example `... Deathly Hallows, Part 1`) and would be split apart by the cell format, so adjacent pieces are also tried joined together.
- Simplified Chinese labels are preferred, then other Chinese variants. Anything without a Chinese label **keeps its English name**.

## Configuration

Constants at the top of each script:

| Script | Constant | Default | Meaning |
|---|---|---|---|
| 02 | `ONLY_ACTORS` | `True` | Only actors are marked; set `False` to include directors, writers, etc. |
| 02 | `VERY_FAMOUS_MIN` | `40` | Wikipedia editions needed for "very famous" |
| 02 | `FAMOUS_MIN` | `15` | Wikipedia editions needed for "famous" |
| 02 | `WORKS_VERY_FAMOUS` / `WORKS_FAMOUS` | `3` / `1` | Number of best-known works to list |
| 02 | `COLOR_VERY_FAMOUS` / `COLOR_FAMOUS` | green / blue | Colours (ARGB hex) |
| 02, 03 | `WORKERS` | `5` | Parallel Wikidata lookups; too many can trigger rate limiting |
| 03 | `ZH_LANGS` | Simplified first | Order of preference for Chinese label variants |

## Known limitations

- **The fame score is a heuristic.** "Number of Wikipedia editions" measures international recognition, not popularity in any particular country or audience.
- **Wikidata coverage is uneven.** Critics, archivists and restorers are often missing, and newer or lesser-known films often have no Chinese title (they stay in English).
- **Name matching can be wrong.** Same-name film people exist; the console prints a warning when the match is uncertain, and the Wikidata id is stored in the cache so you can check it.
- **Regex name extraction has limits.** Unusual tooltip wording can produce a missed or malformed name. Spot-check against the tooltip text.
- **Step 1 is tied to the site's current markup.** If Film at Lincoln Center changes their page, the selectors (for example `a > div`, `div.z-50 > p`) will need updating. Hovering can also be flaky on a slow connection.

## Responsible use

- Intended for personal, non-commercial use. Check the site's terms of use and `robots.txt` before running it.
- Step 1 loads a single page and hovers over the showtime buttons one at a time with short pauses; please do not modify it to hit the site aggressively.
- Do not republish the scraped festival content. Wikidata's data is released under CC0.
