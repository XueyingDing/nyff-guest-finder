"""Regression tests for scripts/02_lookup_famous_guests.py.

Covers: a person genuinely not found on Wikidata vs. a request that failed outright
(must not be confused); a person identified but whose "notable works" lookup failed
(must not discard the identification); a failed lookup must not be cached and must be
retried on the next run; a successfully cached lookup must NOT trigger a new network
call on the next run; an input CSV with no data rows must fail with a clear message
instead of an IndexError.

Nothing here reads or writes anything under results/ or the repo root -- every path
is under pytest's tmp_path.
"""
import csv
import json
import os

import pytest

from conftest import load_script


@pytest.fixture
def module():
    return load_script("02_lookup_famous_guests.py")


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["Title", "Director", "Guests", "Sessions"])
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------- lookup_person()

def test_lookup_person_not_found_is_a_real_negative(module):
    module.get_json = lambda url, params, retries=3: {"search": []}
    result = module.lookup_person("Nobody Famous")
    assert result["status"] == "not_found"
    assert result["note"] == "not found on Wikidata"


def test_lookup_person_propagates_request_failure(module):
    def always_fails(url, params, retries=3):
        raise module.LookupUnavailable("simulated outage")
    module.get_json = always_fails
    with pytest.raises(module.LookupUnavailable):
        module.lookup_person("Anyone")


def test_lookup_person_ok_but_works_lookup_fails_is_not_discarded(module):
    """Person identification succeeds; only the notable-works SPARQL call fails.
    The identification must still come back as status 'ok' with works_incomplete=True,
    not be thrown away as a whole-lookup failure."""
    entity = {
        "claims": {
            "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}],
            "P106": [{"mainsnak": {"datavalue": {"value": {"id": "Q33999"}}}}],  # actor
        },
        "labels": {"en": {"value": "Famous Person"}},
        "sitelinks": {f"lang{i}wiki": {} for i in range(20)},  # 20 >= FAMOUS_MIN
    }

    def fake_get_json(url, params, retries=3):
        action = params.get("action")
        if action == "wbsearchentities":
            return {"search": [{"id": "Q1"}]}
        if action == "wbgetentities":
            return {"entities": {"Q1": entity}}
        raise module.LookupUnavailable("sparql endpoint down")  # top_works() SPARQL call

    module.get_json = fake_get_json
    result = module.lookup_person("Famous Person")
    assert result["status"] == "ok"
    assert result["qid"] == "Q1"
    assert result["is_actor"] is True
    assert result["works"] == []
    assert result["works_incomplete"] is True
    assert "works lookup failed" in result["note"]


# ---------------------------------------------------------------- guest_tier() / build_works()

def test_pending_lookup_is_not_rendered_as_not_famous(module):
    cache = {
        "Failed Guest": {"status": "error"},
        "Star": {"status": "ok", "qid": "Q2", "is_actor": True, "wiki_langs": 50, "works": ["Movie"]},
        "Unknown Person": {"status": "not_found"},
    }
    assert module.guest_tier("Failed Guest", cache) == "pending"
    assert module.guest_tier("Star", cache) == "very"
    assert module.guest_tier("Unknown Person", cache) is None

    built = module.build_works(["Failed Guest", "Star", "Unknown Person"], cache)
    assert built["lookup_pending"] == "Failed Guest"
    assert "Star: Movie" in built["very_famous_actors_works"]
    assert "Failed Guest" not in built["very_famous_actors_works"]
    assert "Unknown Person" not in built["famous_actors_works"]


# ---------------------------------------------------------------- main() integration

def test_main_does_not_cache_a_failed_lookup_and_retries_it_next_run(module, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    module.IN_FILE = str(tmp_path / "nyff64_films.csv")
    module.OUT_FILE = str(tmp_path / "nyff64_films.xlsx")
    module.CACHE_FILE = str(tmp_path / "wikidata_cache_v2.json")
    write_csv(module.IN_FILE, [
        {"Title": "Test Film", "Director": "D. Rector", "Guests": "Flaky Guest",
         "Sessions": "6:00 PM Q&A: Flaky Guest"},
    ])

    calls = {"n": 0}

    def flaky_lookup(name):
        calls["n"] += 1
        raise module.LookupUnavailable("rate limited")

    module.lookup_person = flaky_lookup
    module.main()

    assert calls["n"] == 1
    # A cache file need not even exist yet if every lookup this run failed -- but if it
    # does, the failed guest must not be in it.
    if os.path.exists(module.CACHE_FILE):
        with open(module.CACHE_FILE, encoding="utf-8") as f:
            cache = json.load(f)
        assert "Flaky Guest" not in cache  # a failed request must never be cached as a result

    def recovered_lookup(name):
        calls["n"] += 1
        return {"guest": name, "status": "ok", "qid": "Q9", "is_actor": True,
                 "wiki_langs": 60, "works": ["Big Movie"], "works_incomplete": False, "note": ""}

    module.lookup_person = recovered_lookup
    module.main()

    assert calls["n"] == 2  # retried on the second run, since the first run's failure wasn't cached
    with open(module.CACHE_FILE, encoding="utf-8") as f:
        cache = json.load(f)
    assert cache["Flaky Guest"]["status"] == "ok"
    assert cache["Flaky Guest"]["qid"] == "Q9"


def test_main_reuses_a_successful_cache_entry_without_a_new_lookup(module, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    module.IN_FILE = str(tmp_path / "nyff64_films.csv")
    module.OUT_FILE = str(tmp_path / "nyff64_films.xlsx")
    module.CACHE_FILE = str(tmp_path / "wikidata_cache_v2.json")
    write_csv(module.IN_FILE, [
        {"Title": "Test Film", "Director": "D. Rector", "Guests": "Already Known Star",
         "Sessions": "6:00 PM Q&A: Already Known Star"},
    ])
    with open(module.CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump({"Already Known Star": {
            "guest": "Already Known Star", "status": "ok", "qid": "Q5", "is_actor": True,
            "wiki_langs": 80, "works": ["Famous Movie"], "works_incomplete": False, "note": "",
        }}, f)

    def must_not_be_called(name):
        raise AssertionError(f"lookup_person() should not be called for a cached guest, got {name!r}")

    module.lookup_person = must_not_be_called
    module.main()  # must not raise -- proves the cached entry was reused, not re-queried

    assert os.path.exists(module.OUT_FILE)


def test_main_rejects_empty_csv_with_a_clear_message_not_a_crash(module, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    module.IN_FILE = str(tmp_path / "nyff64_films.csv")
    module.OUT_FILE = str(tmp_path / "nyff64_films.xlsx")
    module.CACHE_FILE = str(tmp_path / "wikidata_cache_v2.json")
    write_csv(module.IN_FILE, [])  # header only, zero data rows

    with pytest.raises(SystemExit) as excinfo:
        module.main()
    assert "no film" in str(excinfo.value).lower() or "nothing to look up" in str(excinfo.value).lower()


def test_one_guests_unexpected_error_does_not_abort_the_rest_of_the_batch(module, tmp_path, monkeypatch):
    """Regression for a real incident: a print() of a guest's name crashed with
    UnicodeEncodeError (a console-codepage limitation, not a script 02 bug in itself)
    partway through a 165-guest run. Because that print sat inside the `with
    ThreadPoolExecutor(...) as pool:` block, the exception had to propagate through
    the pool's __exit__ (shutdown(wait=True)), which doesn't cancel already-submitted
    work -- so every other in-flight lookup kept burning real API calls for nothing,
    saved nothing, and the run eventually crashed anyway. The per-result processing
    must be wrapped so one guest's unexpected failure can't do that to the whole batch."""
    monkeypatch.chdir(tmp_path)
    module.IN_FILE = str(tmp_path / "nyff64_films.csv")
    module.OUT_FILE = str(tmp_path / "nyff64_films.xlsx")
    module.CACHE_FILE = str(tmp_path / "wikidata_cache_v2.json")
    write_csv(module.IN_FILE, [
        {"Title": "Test Film", "Director": "D. Rector", "Guests": "Trouble Guest; Fine Guest",
         "Sessions": "6:00 PM Q&A: Trouble Guest, Fine Guest"},
    ])

    def flaky_lookup(name):
        if name == "Trouble Guest":
            return {"guest": name, "status": "ok", "qid": "Q1", "is_actor": True,
                     "wiki_langs": 50, "works": [], "works_incomplete": False, "note": ""}
        return {"guest": name, "status": "ok", "qid": "Q2", "is_actor": True,
                 "wiki_langs": 60, "works": [], "works_incomplete": False, "note": ""}

    real_json_dump = module.json.dump
    calls = {"n": 0}

    def flaky_json_dump(obj, f, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated write failure for the first result")
        return real_json_dump(obj, f, **kwargs)

    module.lookup_person = flaky_lookup
    monkeypatch.setattr(module.json, "dump", flaky_json_dump)
    module.main()  # must not raise -- one bad result must not sink the whole run

    with open(module.CACHE_FILE, encoding="utf-8") as f:
        cache = json.load(f)
    # The second guest processed must still have been saved even though the first
    # one's write blew up; exactly which guest hits the simulated failure depends on
    # thread scheduling, so check that at least one of the two made it to disk.
    assert len(cache) >= 1
    assert os.path.exists(module.OUT_FILE)
