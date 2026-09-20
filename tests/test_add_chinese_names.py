"""Regression tests for scripts/03_add_chinese_names.py.

Covers: a Wikidata request failure must propagate (not silently look like "no Chinese
name found"); when an actor's identity is already known but the per-title SPARQL query
fails, the identity must still be cached and only the missing titles retried next run
(a failure in one must not discard success in the other); a failed actor lookup must
not be cached and must be retried; a successfully cached actor must not trigger a new
lookup on the next run; and a *legacy* actor cache entry from before the zh_checked
marker existed -- qid known, zh empty, ambiguous whether that's a genuine "no Chinese
name" or a swallowed 429 during the label lookup -- gets its label re-verified exactly
once (keeping the known qid, no re-search), a failure there doesn't get cached either,
and a genuinely-empty result is trusted afterwards and not queried again.

Nothing here reads or writes anything under results/ or the repo root.
"""
import json
import os

import pytest
from openpyxl import Workbook

from conftest import load_script


@pytest.fixture
def module():
    return load_script("03_add_chinese_names.py")


def write_source_xlsx(path, very_famous_cell="", famous_cell=""):
    wb = Workbook()
    ws = wb.active
    ws.append(["Title", "Guests", "Very Famous Actors & Work", "Famous Actors & Work", "Sessions"])
    ws.append(["Test Film", "Adam Driver", very_famous_cell, famous_cell, "6:00 PM Q&A: Adam Driver"])
    wb.save(path)


# ---------------------------------------------------------------- find_actor()

def test_find_actor_propagates_request_failure(module):
    def always_fails(name):
        raise module.LookupUnavailable("simulated outage")
    module.search_qid = always_fails
    with pytest.raises(module.LookupUnavailable):
        module.find_actor("Someone", old_cache={})


def test_find_actor_genuine_not_found_is_a_real_negative(module):
    module.search_qid = lambda name: ""
    result = module.find_actor("Nobody Famous", old_cache={})
    assert result == {"qid": "", "zh": "", "zh_checked": True}


def test_find_actor_marks_zh_checked_on_success(module):
    module.search_qid = lambda name: "Q1"
    module.fetch_zh_label = lambda qid: "名字"
    result = module.find_actor("Someone Famous", old_cache={})
    assert result == {"qid": "Q1", "zh": "名字", "zh_checked": True}


# ---------------------------------------------------------------- main() integration

def test_actor_identity_kept_when_only_works_lookup_fails(module, tmp_path, monkeypatch):
    """query_films() fails for an actor whose identity was just resolved: the actor
    must still be cached (their Chinese name is known), and only the missing titles
    should remain un-cached so they're retried on the next run."""
    monkeypatch.chdir(tmp_path)
    module.IN_FILE = str(tmp_path / "nyff64_films.xlsx")
    module.OUT_FILE = str(tmp_path / "nyff64_films_with_chinese.xlsx")
    module.CACHE_FILE = str(tmp_path / "zh_lookup_cache.json")
    module.OLD_CACHE_FILE = str(tmp_path / "wikidata_cache_v2.json")
    write_source_xlsx(module.IN_FILE, very_famous_cell="Adam Driver: Marriage Story, Paterson")

    module.find_actor = lambda name, old_cache: {"qid": "Q4678990", "zh": "亚当·德赖弗", "zh_checked": True}

    def failing_query_films(qid, titles):
        raise RuntimeError("sparql endpoint down")

    module.query_films = failing_query_films
    module.main()

    with open(module.CACHE_FILE, encoding="utf-8") as f:
        cache = json.load(f)
    assert cache["actors"]["Adam Driver"]["qid"] == "Q4678990"  # identity kept despite works failure
    assert cache["works"] == {}  # nothing wrongly cached as "not found" for the failed titles

    # Second run: query_films now works. The missing titles should be retried (their key
    # was never written), while the actor identity lookup should NOT be repeated.
    def must_not_be_called(name, old_cache):
        raise AssertionError("find_actor() should not be called again; actor is already cached")

    module.find_actor = must_not_be_called
    module.query_films = lambda qid, titles: {t: f"{t}(zh)" for t in titles}
    module.main()

    with open(module.CACHE_FILE, encoding="utf-8") as f:
        cache = json.load(f)
    # candidates_for() expands ["Marriage Story", "Paterson"] to 3 candidate strings
    # (each title plus the comma-joined pair, in case the real title contains a comma).
    assert len(cache["works"]) == 3
    assert all(v["found"] for v in cache["works"].values())


def test_main_does_not_cache_a_failed_actor_lookup_and_retries_it(module, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    module.IN_FILE = str(tmp_path / "nyff64_films.xlsx")
    module.OUT_FILE = str(tmp_path / "nyff64_films_with_chinese.xlsx")
    module.CACHE_FILE = str(tmp_path / "zh_lookup_cache.json")
    module.OLD_CACHE_FILE = str(tmp_path / "wikidata_cache_v2.json")
    write_source_xlsx(module.IN_FILE, very_famous_cell="Flaky Actor: Some Movie")

    calls = {"n": 0}

    def flaky_find_actor(name, old_cache):
        calls["n"] += 1
        raise module.LookupUnavailable("rate limited")

    module.find_actor = flaky_find_actor
    module.main()

    assert calls["n"] == 1
    if os.path.exists(module.CACHE_FILE):
        with open(module.CACHE_FILE, encoding="utf-8") as f:
            cache = json.load(f)
        assert "Flaky Actor" not in cache["actors"]  # a failed request must never be cached as a result

    def recovered_find_actor(name, old_cache):
        calls["n"] += 1
        return {"qid": "Q123", "zh": "演员", "zh_checked": True}

    module.find_actor = recovered_find_actor
    module.query_films = lambda qid, titles: {}
    module.main()

    assert calls["n"] == 2  # retried, since the first run's failure wasn't cached
    with open(module.CACHE_FILE, encoding="utf-8") as f:
        cache = json.load(f)
    assert cache["actors"]["Flaky Actor"]["qid"] == "Q123"


def test_main_reuses_a_successfully_cached_actor(module, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    module.IN_FILE = str(tmp_path / "nyff64_films.xlsx")
    module.OUT_FILE = str(tmp_path / "nyff64_films_with_chinese.xlsx")
    module.CACHE_FILE = str(tmp_path / "zh_lookup_cache.json")
    module.OLD_CACHE_FILE = str(tmp_path / "wikidata_cache_v2.json")
    write_source_xlsx(module.IN_FILE, very_famous_cell="Cached Actor: Some Movie")
    with open(module.CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump({"actors": {"Cached Actor": {"qid": "Q1", "zh": "已知演员", "zh_checked": True}},
                   "works": {"Cached Actor||Some Movie": {"found": True, "zh": "某电影"}}}, f)

    def must_not_be_called(*args, **kwargs):
        raise AssertionError("find_actor()/fetch_zh_label() should not be called for an already-cached actor")

    module.find_actor = must_not_be_called
    module.fetch_zh_label = must_not_be_called
    module.query_films = must_not_be_called
    module.main()  # must not raise -- proves the cache was reused, not re-queried


# --------------------------------------------- legacy "qid known, zh empty" cache entries
#
# Before this script tagged a completed label lookup with zh_checked=True, a repeated 429
# during the *label* lookup (identity already resolved) could be silently cached as an
# empty zh -- indistinguishable, by looking at the data alone, from an actor who genuinely
# has no Chinese Wikidata label. These tests cover the fix: such a legacy entry gets its
# label re-verified exactly once (keeping the known qid, no re-search); a failure during
# that re-verification is not cached either, so it's retried again next run; and a
# genuinely-empty result, once confirmed, is trusted and not queried again.

def write_legacy_actor_cache(path, qid="Q999", zh=""):
    """A cache entry as an older version of this script would have written it: no
    zh_checked field at all, whatever qid/zh it happened to have."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"actors": {"Legacy Actor": {"qid": qid, "zh": zh}}, "works": {}}, f)


def test_legacy_entry_reverifies_label_keeping_qid_without_full_research(module, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    module.IN_FILE = str(tmp_path / "nyff64_films.xlsx")
    module.OUT_FILE = str(tmp_path / "nyff64_films_with_chinese.xlsx")
    module.CACHE_FILE = str(tmp_path / "zh_lookup_cache.json")
    module.OLD_CACHE_FILE = str(tmp_path / "wikidata_cache_v2.json")
    write_source_xlsx(module.IN_FILE, very_famous_cell="Legacy Actor: Some Movie")
    write_legacy_actor_cache(module.CACHE_FILE, qid="Q999", zh="")

    def must_not_be_called(*args, **kwargs):
        raise AssertionError("find_actor() must not redo the identity search -- the qid is already known")

    module.find_actor = must_not_be_called
    calls = []
    module.fetch_zh_label = lambda qid: (calls.append(qid), "真名字")[1]
    module.query_films = lambda qid, titles: {}
    module.main()

    assert calls == ["Q999"]  # re-fetched the label for the known qid, nothing more
    with open(module.CACHE_FILE, encoding="utf-8") as f:
        cache = json.load(f)
    assert cache["actors"]["Legacy Actor"] == {"qid": "Q999", "zh": "真名字", "zh_checked": True}


def test_legacy_entry_label_reverify_failure_is_not_cached_and_retries(module, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    module.IN_FILE = str(tmp_path / "nyff64_films.xlsx")
    module.OUT_FILE = str(tmp_path / "nyff64_films_with_chinese.xlsx")
    module.CACHE_FILE = str(tmp_path / "zh_lookup_cache.json")
    module.OLD_CACHE_FILE = str(tmp_path / "wikidata_cache_v2.json")
    write_source_xlsx(module.IN_FILE, very_famous_cell="Legacy Actor: Some Movie")
    write_legacy_actor_cache(module.CACHE_FILE, qid="Q999", zh="")

    module.find_actor = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not re-search"))
    module.fetch_zh_label = lambda qid: (_ for _ in ()).throw(module.LookupUnavailable("rate limited"))
    module.query_films = lambda qid, titles: {}
    module.main()

    with open(module.CACHE_FILE, encoding="utf-8") as f:
        cache = json.load(f)
    # Still ambiguous -- must NOT have been cached as a confirmed empty label.
    assert "zh_checked" not in cache["actors"]["Legacy Actor"]
    assert cache["actors"]["Legacy Actor"]["qid"] == "Q999"  # qid is untouched, not lost

    # Next run recovers: the same legacy entry must be retried again (not skipped).
    calls = []
    module.fetch_zh_label = lambda qid: (calls.append(qid), "真名字")[1]
    module.main()
    assert calls == ["Q999"]
    with open(module.CACHE_FILE, encoding="utf-8") as f:
        cache = json.load(f)
    assert cache["actors"]["Legacy Actor"]["zh_checked"] is True


def test_genuinely_empty_label_is_trusted_and_not_requeried(module, tmp_path, monkeypatch):
    """Once a label lookup has genuinely completed (zh_checked=True), even with an empty
    result, it must not be queried again on a later run."""
    monkeypatch.chdir(tmp_path)
    module.IN_FILE = str(tmp_path / "nyff64_films.xlsx")
    module.OUT_FILE = str(tmp_path / "nyff64_films_with_chinese.xlsx")
    module.CACHE_FILE = str(tmp_path / "zh_lookup_cache.json")
    module.OLD_CACHE_FILE = str(tmp_path / "wikidata_cache_v2.json")
    write_source_xlsx(module.IN_FILE, very_famous_cell="No Chinese Name Actor: Some Movie")
    with open(module.CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump({"actors": {"No Chinese Name Actor": {"qid": "Q42", "zh": "", "zh_checked": True}},
                   "works": {}}, f)

    def must_not_be_called(*args, **kwargs):
        raise AssertionError("a confirmed (zh_checked=True) empty label must not be re-queried")

    module.find_actor = must_not_be_called
    module.fetch_zh_label = must_not_be_called
    module.query_films = lambda qid, titles: {}
    module.main()  # must not raise
