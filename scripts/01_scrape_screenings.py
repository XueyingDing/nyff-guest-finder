# Step 1 - scrape the NYFF64 lineup page.
# Opens the lineup in a real browser, hovers over every "Intro" / "Q&A" showtime button,
# reads the tooltip to find out who is attending, and writes nyff64_films.csv
# (only films that have an Intro or Q&A are kept).
#
# Setup: pip install playwright && playwright install chromium
import os
import re
import csv
from playwright.sync_api import sync_playwright

URL = "https://www.filmlinc.org/nyff/nyff64-lineup/?tab=films"
OUT_FILE = "nyff64_films.csv"
FIELDS = ["Title", "Director", "Guests", "Sessions"]   # CSV header (capitalized)

# No longer relies on fixed nth-child positions:
# find every link that points to /films/<slug>, climb up to the largest container
# that holds only that one film, and tag it with data-film-card.
# This way the first film (Paper Tiger) is no longer missed.
MARK_JS = r"""
() => {
  const okHref = a => /\/films\/[^\/?#]+/.test(a.getAttribute('href') || '');
  const norm = a => a.getAttribute('href').split('?')[0].split('#')[0].replace(/\/+$/, '');
  const root = document.querySelector('.space-y-12') || document;
  const links = [...root.querySelectorAll("a[href*='/films/']")].filter(okHref);
  const seen = new Set();
  let idx = 0;
  for (const a of links) {
    const h = norm(a);
    if (seen.has(h)) continue;
    seen.add(h);
    let el = a;
    while (el.parentElement) {
      const p = el.parentElement;
      const hs = new Set([...p.querySelectorAll("a[href*='/films/']")].filter(okHref).map(norm));
      if (hs.size > 1) break;
      el = p;
    }
    el.setAttribute('data-film-card', String(idx++));
  }
  return idx;
}
"""


def extract_guests(text):
    """Extract only the guest names from text like 'Q&A w. Fred Camper and restoration coordinator Kyle Westphal • ...'"""
    first = text.split("•")[0]
    # Take what follows "w." / "with" / "by"; also handles "Introduction + Screening with James Gray"
    m = re.search(r"\b(?:w\.|with|by)\s+(.+)", first, re.I)
    if not m:
        return []
    names_part = m.group(1)
    parts = re.split(r",\s*|\s+and\s+|\s*&\s*", names_part)
    name_re = re.compile(
        r"[A-Z][\w'’.\-]*(?:\s+(?:(?:de|da|del|van|von|der|di|la|le|bin|al)\s+)?[A-Z][\w'’.\-]*)*"
    )
    guests = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        found = name_re.findall(p)
        if found:
            guests.append(found[-1].strip())  # Keep the last capitalized run, dropping lowercase titles like "restoration coordinator"
    return guests


def read_popup(page, btn, attempts=3):
    """Hover over the button and read the popup text; hovering occasionally fails to open the popup, so retry a few times."""
    popup_sel = "div.z-50 > p:nth-child(1):visible"
    for _ in range(attempts):
        try:
            # Scroll the button to the middle of the screen so a sticky top nav bar can't cover it
            btn.evaluate("el => el.scrollIntoView({block: 'center', inline: 'center'})")
            page.wait_for_timeout(400)
            box = btn.bounding_box()
            if not box:
                raise RuntimeError("Button has no bounding box")
            x = box["x"] + box["width"] / 2
            y = box["y"] + box["height"] / 2
            # Move away first so the old popup closes, then move back in to make sure a hover event fires
            page.mouse.move(0, 0)
            page.wait_for_timeout(300)
            page.mouse.move(x - 6, y)
            page.mouse.move(x, y, steps=5)
            popup = page.locator(popup_sel).last  # Take the last one among the visible popups only
            popup.wait_for(state="visible", timeout=2500)
            return popup.inner_text().strip()
        except Exception:
            continue
    total = page.locator("div.z-50").count()
    vis = page.locator("div.z-50:visible").count()
    raise RuntimeError(f"Popup did not appear after {attempts} attempts "
                       f"({total} div.z-50 elements on the page, {vis} visible)")


def save_csv(rows):
    with open(OUT_FILE, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"\nSaved {len(rows)} films to: {os.path.abspath(OUT_FILE)}")


def main():
    rows = []  # Only films that have an Intro or Q&A
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            page = browser.new_page()
            page.goto(URL, wait_until="networkidle")
            page.wait_for_selector(".space-y-12 a[href*='/films/']")

            # Scroll to the bottom so all lazy-loaded content appears
            last_h = 0
            while True:
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(800)
                h = page.evaluate("document.body.scrollHeight")
                if h == last_h:
                    break
                last_h = h
            page.evaluate("window.scrollTo(0, 0)")

            n = page.evaluate(MARK_JS)
            print(f"Found {n} films")

            for i in range(n):
                card = page.locator(f'[data-film-card="{i}"]')

                title_loc = card.locator("a > div").first
                title = title_loc.inner_text().strip() if title_loc.count() else f"(card {i})"

                director = ""
                d_loc = card.locator("a + p").first
                if d_loc.count():
                    director = d_loc.inner_text().strip()

                guests = []      # De-duplicated, in order of appearance
                sessions = []
                found_special = False

                buttons = card.locator("button")
                for j in range(buttons.count()):
                    btn = buttons.nth(j)

                    if not btn.is_visible():  # Skip invisible buttons
                        continue

                    label = btn.inner_text().strip().replace("\n", " ")
                    low = label.lower()
                    if not ("intro" in low or "q&a" in low or "q & a" in low):
                        continue
                    found_special = True

                    session_guests = []
                    try:
                        txt = read_popup(page, btn)
                        session_guests = extract_guests(txt)
                        for g in session_guests:
                            if g not in guests:
                                guests.append(g)
                    except Exception as e:
                        msg = " / ".join(str(e).splitlines()[:4])
                        print(f"[{title}] '{label}' could not read popup: {msg}")
                    finally:
                        page.mouse.move(0, 0)  # Move the mouse away to close the popup
                        page.wait_for_timeout(300)

                    # Record each session together with its own guests, e.g. "6:30 PM Intro: James Gray"
                    if session_guests:
                        sessions.append(f"{label}: {', '.join(session_guests)}")
                    else:
                        sessions.append(label)

                if found_special:
                    row = {
                        "Title": title,
                        "Director": director,
                        "Guests": "; ".join(guests),
                        "Sessions": " | ".join(sessions),
                    }
                    rows.append(row)
                    print(f"[{i + 1}/{n}] {title} | {row['Guests']}")
                else:
                    print(f"[{i + 1}/{n}] {title} (no Intro/Q&A, skipped)")

            browser.close()
    finally:
        # Save whatever was scraped, whether the run finishes normally, errors out, or is stopped with Ctrl+C
        if rows:
            save_csv(rows)
        else:
            print("No films with an Intro or Q&A were found; no CSV written")


if __name__ == "__main__":
    main()
