"""One-off thumbnail-backlog burner. The in-app worker drains newest-first;
this drains oldest-first, so the two meet in the middle with no contention.
Exits when the queue is empty.

    nohup .venv/bin/python burn_thumbs.py > /tmp/burn_thumbs.log 2>&1 &
"""
from app import db, thumbs
from app.config import get_settings


def main() -> None:
    conn = db.connect(get_settings().db_path)
    total = 0
    while True:
        done = thumbs.process_pending(conn, limit=20, oldest_first=True)
        if done == 0:
            break
        total += done
        if total % 100 < 20:
            left = conn.execute(
                "SELECT COUNT(*) FROM media WHERE thumb_key IS NULL "
                "AND thumb_attempts < 2").fetchone()[0]
            print(f"burned {total}, ~{left} left", flush=True)
    print(f"backlog drained: {total} thumbnails generated", flush=True)


if __name__ == "__main__":
    main()
