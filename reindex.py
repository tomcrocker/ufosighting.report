"""Full Meilisearch rebuild: apply settings, then index every public sighting.
Run after deploys that change index settings, and after backfills:
    python reindex.py [--wipe]
"""
import sys

import httpx

from app import db, search
from app.config import get_settings


def main(wipe: bool = False) -> None:
    if not search.enabled():
        print("MEILI_URL not set — nothing to do")
        return
    s = get_settings()
    url = s.meili_url.rstrip("/")
    headers = {"Authorization": f"Bearer {s.meili_key}"}
    if wipe:
        httpx.delete(f"{url}/indexes/{s.meili_index}", headers=headers, timeout=30)
        print(f"wiped index {s.meili_index}")
    search.apply_settings()
    conn = db.connect(s.db_path)
    try:
        statuses = ",".join(f"'{st}'" for st in search.PUBLIC_STATUSES)
        ids = [r["id"] for r in conn.execute(
            f"SELECT id FROM sightings WHERE status IN ({statuses}) ORDER BY id")]
        for i in range(0, len(ids), 500):
            search.index_sightings(conn, ids[i:i + 500])
        print(f"reindexed {len(ids)} sightings")
    finally:
        conn.close()


if __name__ == "__main__":
    main(wipe="--wipe" in sys.argv)
