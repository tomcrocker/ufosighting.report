"""One-shot: generate <=2048px JPEG display derivatives for oversized ingested
images. Reddit-archived originals run up to several MB and the detail viewer
served them directly, which made sighting pages feel sluggish — the viewer
prefers display_url when present (template unchanged), and the original stays
for download/analysis.

Safe to re-run: only touches image rows with no display_key. Fetched byte size
is written back to size_bytes either way, so small originals (<= DISPLAY_BYTES)
drop out of the candidate query on the next pass instead of being re-fetched.
GIFs are skipped — a JPEG derivative would freeze the animation."""
import sys
import time

import httpx

from app import db, r2
from app.config import get_settings
from app.thumbs import DISPLAY_BYTES, DISPLAY_MAX, generate_image_thumb


def candidates(conn):
    return conn.execute(
        """SELECT id, r2_key FROM media
           WHERE kind='image' AND display_key IS NULL
             AND lower(r2_key) NOT LIKE '%.gif'
             AND (size_bytes IS NULL OR size_bytes > ?)
           ORDER BY id""", (DISPLAY_BYTES,)).fetchall()


def process_one(conn, row, *, fetch=None) -> str:
    """Returns 'derived' | 'small' | 'failed' (kept re-runnable on failure)."""
    fetch = fetch or (lambda url: httpx.get(url, timeout=60,
                                            follow_redirects=True).content)
    try:
        data = fetch(r2.public_url(row["r2_key"]))
        if len(data) <= DISPLAY_BYTES:
            conn.execute("UPDATE media SET size_bytes=? WHERE id=?",
                         (len(data), row["id"]))
            conn.commit()
            return "small"
        display = generate_image_thumb(data, DISPLAY_MAX, 88)
        rest = row["r2_key"].split("/", 1)[1]
        display_key = "display/" + rest.rsplit(".", 1)[0] + ".jpg"
        r2.put_bytes(display_key, display, "image/jpeg")
        conn.execute("UPDATE media SET display_key=?, size_bytes=? WHERE id=?",
                     (display_key, len(data), row["id"]))
        conn.commit()
        return "derived"
    except Exception as exc:
        print(f"burn_displays: media {row['id']} ({row['r2_key']}) failed: {exc}",
              flush=True)
        return "failed"


def main(limit: int | None = None) -> None:
    conn = db.connect(get_settings().db_path)
    try:
        rows = candidates(conn)
        if limit:
            rows = rows[:limit]
        stats = {"derived": 0, "small": 0, "failed": 0}
        print(f"burn_displays: {len(rows)} candidates", flush=True)
        for i, row in enumerate(rows, 1):
            stats[process_one(conn, row)] += 1
            if i % 100 == 0:
                print(f"burn_displays: {i}/{len(rows)} {stats}", flush=True)
            time.sleep(0.15)  # gentle on the 1GB box; web serving comes first
        print(f"burn_displays DONE: {stats}", flush=True)
    finally:
        conn.close()


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else None)
