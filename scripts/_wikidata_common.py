# Shared HTTP helper for the two Wikidata lookup scripts
# (02_lookup_famous_guests.py and 03_add_chinese_names.py).
#
# get_json() used to have a bug: if Wikidata kept returning HTTP 429 (rate limited)
# for every retry, the function fell through to `return {}` instead of raising -- so
# a request that never actually succeeded looked exactly like a request that
# succeeded and legitimately found nothing. Callers then cached that as a permanent
# "not found". This module fixes that: a request either returns real JSON from a
# successful response, or raises LookupUnavailable. It never fabricates an empty result.
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

NOT_LANG_WIKI = {"commonswiki", "specieswiki", "metawiki", "wikidatawiki", "mediawikiwiki"}


def make_stdout_unicode_safe():
    """Make console output tolerant of characters the terminal's codepage can't show.

    Both scripts print guest/actor names and (in 03) Chinese labels as they go. On
    Windows, stdout is often still the legacy cp1252 codepage, which can't encode a
    lot of ordinary names (e.g. Polish "ł" isn't in cp1252 either). An uncaught
    UnicodeEncodeError there doesn't just lose one line of output -- when it's raised
    inside the `with ThreadPoolExecutor(...) as pool:` block, the exception has to
    propagate through the pool's __exit__, which waits (shutdown(wait=True)) for every
    already-submitted lookup to finish before it can re-raise. So the run silently
    keeps burning real API calls for everyone still queued, saves none of it (the loop
    that would cache results already died), and only then crashes -- looking, from the
    outside, exactly like a stall. Call this once at start-up in both scripts."""
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="replace")
        except Exception:
            pass


def _describe(url, params):
    action = params.get("action")
    if action:
        target = params.get("search") or params.get("ids") or ""
        return f"{action}({target})" if target else action
    return f"SPARQL query to {url}"


class LookupUnavailable(Exception):
    """The request could not be completed (network error, timeout, or Wikidata kept
    answering 429) after all retries. This is different from a request that
    succeeded and simply matched nothing -- callers must not treat the two the same,
    and must not cache a LookupUnavailable as if it were a real result."""


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


def _retry_after_seconds(resp, default):
    """Parse the Retry-After header (seconds, or an HTTP-date) if present; else `default`."""
    ra = resp.headers.get("Retry-After")
    if not ra:
        return default
    ra = ra.strip()
    try:
        return max(float(ra), 0.0)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(ra)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max((dt - datetime.now(timezone.utc)).total_seconds(), 0.0)
    except Exception:
        pass
    return default


def get_json(url, params, headers, retries=3, backoff=2.0, verbose=False):
    """GET url as JSON.

    Returns the parsed JSON body of a successful (2xx) response -- including a
    response that legitimately contains zero results, e.g. {"search": []}.

    Raises LookupUnavailable if no attempt succeeds within `retries` tries: this
    covers network errors, timeouts, non-2xx responses, and persistent HTTP 429
    (rate limiting). On a 429 it waits for the server's Retry-After header if one
    is given, otherwise a fixed backoff similar to the original behaviour.

    If verbose, prints one line per retry/wait naming what was being looked up, the
    attempt number, the reason, and how long it's about to wait -- so a slow or
    repeatedly-throttled run shows up in the log instead of just looking stalled.
    """
    last_err = None
    what = _describe(url, params)
    for attempt in range(retries):
        is_last = attempt == retries - 1
        try:
            r = requests.get(url, params=params, headers=headers, timeout=40)
        except requests.RequestException as ex:
            last_err = ex
            if is_last:
                break
            wait = backoff * (attempt + 1)
            if verbose:
                print(f"  [retry] {what}: request error on attempt {attempt + 1}/{retries} "
                      f"({ex}); waiting {wait:.0f}s", flush=True)
            time.sleep(wait)
            continue

        if r.status_code == 429:
            last_err = RuntimeError("HTTP 429 (rate limited)")
            if is_last:
                break
            wait = _retry_after_seconds(r, default=5.0 * (attempt + 1))
            if verbose:
                print(f"  [retry] {what}: HTTP 429 on attempt {attempt + 1}/{retries}; "
                      f"waiting {wait:.0f}s", flush=True)
            time.sleep(wait)
            continue

        try:
            r.raise_for_status()
        except requests.RequestException as ex:
            last_err = ex
            if is_last:
                break
            wait = backoff * (attempt + 1)
            if verbose:
                print(f"  [retry] {what}: HTTP {r.status_code} on attempt {attempt + 1}/{retries} "
                      f"({ex}); waiting {wait:.0f}s", flush=True)
            time.sleep(wait)
            continue

        return r.json()

    if verbose:
        print(f"  [failed] {what}: giving up after {retries} attempt(s): {last_err}", flush=True)
    raise LookupUnavailable(f"{url} failed after {retries} attempt(s): {last_err}") from last_err
