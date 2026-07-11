"""Queue CLI for the YouTube download worker on the local VM.

The worker SSHes in and runs (cwd matters — .env loads relative to cwd):
    cd /home/ubuntu/ufosighting && .venv/bin/python ytq.py claim
    ... ytq.py done <job_id> --key uploads/2026/07/yt_abc.mp4 --size 12345
    ... ytq.py fail <job_id> --error "yt-dlp: video unavailable"
"""
import argparse
import json

from app import db, search
from app.config import get_settings

MAX_ATTEMPTS = 3
_NOW = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"


def claim(conn, limit: int = 5) -> list[dict]:
    rows = conn.execute(
        "SELECT id, sighting_id, url FROM yt_jobs "
        "WHERE status='pending' AND attempts < ? ORDER BY id LIMIT ?",
        (MAX_ATTEMPTS, limit)).fetchall()
    return [{"job_id": r["id"], "sighting_id": r["sighting_id"], "url": r["url"]}
            for r in rows]


def done(conn, job_id: int, key: str, size: int) -> None:
    job = conn.execute("SELECT sighting_id FROM yt_jobs WHERE id=?", (job_id,)).fetchone()
    if job is None:
        raise SystemExit(f"no such job {job_id}")
    sid = job["sighting_id"]
    order = conn.execute("SELECT COALESCE(MAX(sort_order)+1, 0) FROM media "
                         "WHERE sighting_id=?", (sid,)).fetchone()[0]
    conn.execute("INSERT INTO media (sighting_id, r2_key, kind, size_bytes, sort_order) "
                 "VALUES (?,?,'video',?,?)", (sid, key, size, order))
    conn.execute(f"UPDATE yt_jobs SET status='done', updated_at={_NOW} WHERE id=?",
                 (job_id,))
    conn.commit()
    search.index_sightings(conn, [sid])


def fail(conn, job_id: int, error: str) -> None:
    row = conn.execute("SELECT attempts FROM yt_jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise SystemExit(f"no such job {job_id}")
    attempts = row["attempts"] + 1
    status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
    conn.execute(f"UPDATE yt_jobs SET attempts=?, status=?, last_error=?, "
                 f"updated_at={_NOW} WHERE id=?",
                 (attempts, status, (error or "")[:300], job_id))
    conn.commit()


def main() -> None:
    p = argparse.ArgumentParser(description="YouTube job queue")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("claim")
    c.add_argument("--limit", type=int, default=5)
    d = sub.add_parser("done")
    d.add_argument("job_id", type=int)
    d.add_argument("--key", required=True)
    d.add_argument("--size", type=int, required=True)
    f = sub.add_parser("fail")
    f.add_argument("job_id", type=int)
    f.add_argument("--error", default="")
    args = p.parse_args()
    conn = db.connect(get_settings().db_path)
    try:
        if args.cmd == "claim":
            print(json.dumps(claim(conn, args.limit)))
        elif args.cmd == "done":
            done(conn, args.job_id, args.key, args.size)
            print("ok")
        else:
            fail(conn, args.job_id, args.error)
            print("ok")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
