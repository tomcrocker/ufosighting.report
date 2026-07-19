"""Re-extract sighting DATES for ingested r/UFOs rows using the post-date-aware
prompt (extract.extract_fields now takes post_date). The original ingest ran a
date-blind prompt that guessed relative / year-less dates (usually the wrong
year) or gave up and fell back to the post date. This re-runs extraction with
the post's submission date as an anchor and updates sighted_at + tz_name ONLY
when the new extraction yields a NON-NULL date that differs from what's stored.

Safe by design:
  - Only touches sighted_at + tz_name (NOT location/geo — no re-geocoding).
  - Updates only when the new date is non-null AND differs (never blanks a row,
    never reverts a real date to the post-date fallback).
  - Resume-safe via a state file of processed ids; commit + save per batch.
  - Every change appended to a JSONL changelog for audit.
  - Detects xAI credit/key death (a run of empty API responses) and stops
    cleanly so you can top up and resume.

Uses whatever provider app.config points at (Grok by default on the VM).

  PYTHONPATH=. .venv/bin/python backfill_dates.py --limit 150 --dry-run
  PYTHONPATH=. .venv/bin/python backfill_dates.py --all
"""
import argparse
import json
import os
import time

from app import db, extract, search
from app.config import get_settings
from ingest import build_sighted_at

BATCH = 50
EMPTY_ABORT = 25  # consecutive empty API responses => key/credits dead
CALL_COST = 0.00268  # ~$ per call (grok-4.20-non-reasoning, from prior backfill)


def load_state(path):
    if os.path.exists(path):
        with open(path) as f:
            return set(json.load(f))
    return set()


def save_state(path, done):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(sorted(done), f)
    os.replace(tmp, path)


def op_comments(conn, sid, username):
    return [r[0] for r in conn.execute(
        "SELECT body FROM comments WHERE sighting_id=? AND lower(author)=lower(?) "
        "ORDER BY score DESC LIMIT 3", (sid, username)).fetchall()]


def eligible_rows(conn, *, fallback_only):
    sql = ("SELECT id, title, description, reddit_username, sighted_at, tz_name, "
           "reddit_posted_at, created_at FROM sightings WHERE source='reddit'")
    if fallback_only:
        sql += (" AND reddit_posted_at IS NOT NULL "
                "AND substr(sighted_at,1,10)=substr(reddit_posted_at,1,10)")
    sql += " ORDER BY id"
    return conn.execute(sql).fetchall()


def run(args):
    conn = db.connect(get_settings().db_path)
    done = load_state(args.state)
    rows = eligible_rows(conn, fallback_only=args.fallback_only)
    todo = [r for r in rows if r["id"] not in done]
    if args.limit:
        todo = todo[:args.limit]
    print(f"backfill_dates: {len(rows)} eligible, {len(todo)} to process "
          f"(dry_run={args.dry_run}, fallback_only={args.fallback_only})", flush=True)

    calls = changed = skipped_short = 0
    consecutive_empty = 0
    changed_ids = []
    clog = open(args.changelog, "a")

    for n, r in enumerate(todo, 1):
        sid = r["id"]
        post_iso = r["reddit_posted_at"] or r["created_at"]
        text = extract.combine_post_text(
            {"title": r["title"], "selftext": r["description"]},
            op_comments(conn, sid, r["reddit_username"]))
        if len(text.strip()) < 40:
            skipped_short += 1
            done.add(sid)
            continue

        raw = extract.extract_fields(text, post_date=post_iso)
        calls += 1
        if not raw:  # {} == API failure (a real "no date" post returns {"date":null,...})
            consecutive_empty += 1
            if consecutive_empty >= EMPTY_ABORT:
                print(f"\n!! {EMPTY_ABORT} consecutive empty responses — xAI key/"
                      f"credits likely dead. Stopping at id={sid}. Resume after "
                      f"topping up (state saved).", flush=True)
                break
            continue
        consecutive_empty = 0

        new = extract.validate_and_clamp(raw, post_created_iso=post_iso)
        new_sighted, new_tz = build_sighted_at(new, post_iso)
        old_date = (r["sighted_at"] or "")[:10]
        new_date = new_sighted[:10]

        if new["date"] is not None and new_date != old_date:
            if not args.dry_run:
                # Commit IMMEDIATELY: on a 1GB VM the web app writes analytics_visits
                # on every page view, so we must never hold the write lock across the
                # (slow) API calls of a batch — that stalled page loads up to 30s.
                conn.execute("UPDATE sightings SET sighted_at=?, tz_name=? WHERE id=?",
                             (new_sighted, new_tz, sid))
                conn.commit()
            changed += 1
            changed_ids.append(sid)
            clog.write(json.dumps({
                "id": sid, "title": r["title"][:80], "post_date": post_iso[:10],
                "old": old_date, "new": new_date, "new_tz": new_tz,
                "was_fallback": old_date == (r["reddit_posted_at"] or "")[:10],
            }) + "\n")
            clog.flush()

        done.add(sid)
        if n % BATCH == 0:
            save_state(args.state, done)
            print(f"  {n}/{len(todo)}  calls={calls} changed={changed} "
                  f"short={skipped_short}  (~${calls*CALL_COST:.2f})", flush=True)
        time.sleep(0.1)

    if not args.dry_run:
        conn.commit()
    save_state(args.state, done)
    clog.close()

    if changed_ids and not args.dry_run and search.enabled():
        print(f"reindexing {len(changed_ids)} changed rows...", flush=True)
        for i in range(0, len(changed_ids), 200):
            search.index_sightings(conn, changed_ids[i:i + 200])

    print(f"\nDONE: processed_calls={calls} changed={changed} "
          f"skipped_short={skipped_short} est_cost=${calls*CALL_COST:.2f}", flush=True)
    print(f"changelog: {args.changelog}  state: {args.state}", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="process only first N (test)")
    ap.add_argument("--all", action="store_true", help="process all eligible")
    ap.add_argument("--fallback-only", action="store_true",
                    help="only rows where sighted-date == posted-date (old null-fallback)")
    ap.add_argument("--dry-run", action="store_true", help="extract + log, don't write DB")
    ap.add_argument("--state", default="data/backfill_dates_state.json")
    ap.add_argument("--changelog", default="/tmp/backfill_dates_changes.jsonl")
    a = ap.parse_args()
    if not a.limit and not a.all:
        ap.error("pass --limit N or --all")
    run(a)
