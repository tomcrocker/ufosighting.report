"""Manual rollout helper: preview / post ONE eligible sighting to Bluesky so we
can eyeball it on the account before enabling the auto-sweep.

Needs BSKY_HANDLE + BSKY_APP_PASSWORD in the environment (NOT BSKY_ENABLED — this
bypasses the enabled() gate on purpose so the auto-sweep can stay off while we
test).

  PYTHONPATH=. .venv/bin/python scripts/bsky_test.py             # newest eligible, dry-run
  PYTHONPATH=. .venv/bin/python scripts/bsky_test.py --post      # actually send it
  PYTHONPATH=. .venv/bin/python scripts/bsky_test.py --id 123 --post
"""
import argparse
import sys

from app import bsky, db
from app.config import get_settings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", type=int, help="specific sighting id (default: newest eligible)")
    ap.add_argument("--post", action="store_true", help="actually send (default: dry-run)")
    a = ap.parse_args()

    conn = db.connect(get_settings().db_path)
    if a.id:
        row = conn.execute("SELECT * FROM sightings WHERE id=?", (a.id,)).fetchone()
    else:
        rows = bsky.eligible_rows(conn, limit=1)
        row = rows[0] if rows else None
    if not row:
        print("No eligible sighting found (live + media + geo/substantial, not yet posted).")
        sys.exit(1)

    text, url = bsky.build_post_text(row)
    print(f"--- sighting {row['id']} ---")
    print(text)
    print(f"\nlink: {url}")
    print(f"thumb_key: {bsky._thumb_key(conn, row['id'])}  (chars: {len(text)})")

    if not a.post:
        print("\n(dry-run — pass --post to actually send)")
        return

    s = get_settings()
    if not (s.bsky_handle and s.bsky_app_password):
        print("Set BSKY_HANDLE and BSKY_APP_PASSWORD in the environment first.")
        sys.exit(1)
    session = bsky.create_session()
    uri = bsky.post_sighting(conn, row, session=session)
    conn.execute("UPDATE sightings SET bsky_posted_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                 "WHERE id=?", (row["id"],))
    conn.commit()
    print(f"\nPOSTED: {uri}")
    print("View: https://bsky.app/profile/ufosighting.bsky.social")


if __name__ == "__main__":
    main()
