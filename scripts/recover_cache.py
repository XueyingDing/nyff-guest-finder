# One-off maintenance script: clean up cache files written by an older version of the
# lookup scripts. Before this fix, get_json() could silently return {} after Wikidata
# kept answering HTTP 429 (rate limited), which then looked exactly like "this person
# doesn't exist on Wikidata" / "this title isn't in their filmography" and got cached
# as a permanent (wrong) result.
#
# This script does NOT look anything up online. It only edits the cache files on disk:
#   1. Backs up each cache file it touches (<file>.bak-<timestamp>), unless --dry-run.
#   2. Keeps entries that look trustworthy.
#   3. Drops entries that look like they could have been produced by the bug above, so
#      the next run of 02_lookup_famous_guests.py / 03_add_chinese_names.py looks them
#      up again instead of trusting a possibly-wrong cached negative.
#
# You do not need to delete the whole cache file -- this keeps everything that still
# looks correct and only re-queries what's actually in doubt.
#
# One case needs no file edit at all: a zh_lookup_cache.json actor entry with a known
# qid but an empty Chinese name, written by a version of 03_add_chinese_names.py from
# before it started tagging a completed label lookup with zh_checked=True. This script
# only *reports* those (see "pending re-verification" below); 03_add_chinese_names.py
# detects the missing marker itself and re-fetches just the label next time it runs,
# keeping the existing qid rather than re-searching for the person.
#
# Usage (run from the folder that contains your cache files, usually the repo root):
#   python scripts/recover_cache.py --dry-run     # preview what would change
#   python scripts/recover_cache.py                # actually clean up (backs up first)
import argparse
import json
import os
import shutil
import time

# Kept in sync with the same-named constants in 02_lookup_famous_guests.py: a person
# who clears FAMOUS_MIN and is (or counts as) an actor should have at least one work
# listed. An empty "works" list on such a person is a plausible symptom of the bug too.
FAMOUS_MIN = 15
ONLY_ACTORS = True


def _backup(path):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_path = f"{path}.bak-{stamp}"
    shutil.copy2(path, backup_path)
    return backup_path


def clean_guest_cache(path, dry_run=False):
    """wikidata_cache_v2.json, written by 02_lookup_famous_guests.py."""
    if not os.path.exists(path):
        print(f"[guest cache] {path}: not found, nothing to do")
        return
    with open(path, encoding="utf-8") as f:
        cache = json.load(f)

    kept, dropped = {}, []
    for name, r in cache.items():
        qid = r.get("qid")
        if not qid:
            dropped.append(name)  # "not found": could be real, or a swallowed 429 -- re-verify
            continue
        needs_works = r.get("wiki_langs", 0) >= FAMOUS_MIN and (r.get("is_actor") or not ONLY_ACTORS)
        if needs_works and not r.get("works"):
            dropped.append(name)  # identified, famous enough to have works listed, but none found
            continue
        kept[name] = r

    print(f"[guest cache] {path}: {len(kept)} kept, {len(dropped)} dropped for re-lookup")
    if dropped:
        print("  dropped: " + ", ".join(dropped))
    if dry_run or not dropped:
        return
    backup_path = _backup(path)
    print(f"  backed up to {backup_path}")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(kept, f, ensure_ascii=False, indent=1)


def clean_zh_cache(path, dry_run=False, purge_unmatched_works=False):
    """zh_lookup_cache.json, written by 03_add_chinese_names.py."""
    if not os.path.exists(path):
        print(f"[zh cache] {path}: not found, nothing to do")
        return
    with open(path, encoding="utf-8") as f:
        cache = json.load(f)
    actors = cache.get("actors", {})
    works = cache.get("works", {})

    kept_actors, dropped_actors = {}, []
    pending_zh_reverify = []
    for name, info in actors.items():
        if info.get("qid"):
            kept_actors[name] = info
            if not info.get("zh_checked"):
                pending_zh_reverify.append(name)
        else:
            dropped_actors.append(name)

    kept_works, dropped_works = {}, []
    for key, w in works.items():
        if w.get("found") or not purge_unmatched_works:
            kept_works[key] = w
        else:
            dropped_works.append(key)

    print(f"[zh cache] {path}: {len(kept_actors)} actors kept, {len(dropped_actors)} dropped for re-lookup")
    if dropped_actors:
        print("  dropped actors: " + ", ".join(dropped_actors))
    if pending_zh_reverify:
        print(f"  {len(pending_zh_reverify)} kept actors have a known qid but no 'zh_checked' "
              f"marker (written by a version of 03_add_chinese_names.py before that marker "
              f"existed, so an empty Chinese name there is unverified, not confirmed absent). "
              f"No file changes are needed for these: 03_add_chinese_names.py already detects "
              f"the missing marker on its own and will re-fetch just the Chinese label for "
              f"each one on its next run, keeping the existing qid (no re-search).")
        print("    pending re-verification: " + ", ".join(pending_zh_reverify))
    if purge_unmatched_works:
        print(f"  {len(kept_works)} works kept, {len(dropped_works)} unmatched works dropped for re-verification")
    else:
        print(f"  {len(works)} cached works left untouched "
              f"(pass --purge-unmatched-works to also re-verify 'title not found' results)")

    if dry_run or (not dropped_actors and not dropped_works):
        return
    backup_path = _backup(path)
    print(f"  backed up to {backup_path}")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"actors": kept_actors, "works": kept_works}, f, ensure_ascii=False, indent=1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--guest-cache", default="wikidata_cache_v2.json",
                        help="path to the script-2 cache (default: %(default)s)")
    parser.add_argument("--zh-cache", default="zh_lookup_cache.json",
                        help="path to the script-3 cache (default: %(default)s)")
    parser.add_argument("--purge-unmatched-works", action="store_true",
                        help="also drop cached 'title not in this actor's filmography' results "
                             "so they get re-verified. Off by default: most such results are "
                             "correct, not bug artifacts -- only pass this if you suspect heavy "
                             "429 exposure during a 03_add_chinese_names.py run.")
    parser.add_argument("--dry-run", action="store_true",
                        help="only print what would change; don't touch any files")
    args = parser.parse_args()

    clean_guest_cache(args.guest_cache, dry_run=args.dry_run)
    clean_zh_cache(args.zh_cache, dry_run=args.dry_run, purge_unmatched_works=args.purge_unmatched_works)


if __name__ == "__main__":
    main()
