"""One-off: submit every public URL to IndexNow (Bing, Yandex, Seznam, Naver…).

Mirrors the sitemap exactly — the four static pages plus every public
sighting — and POSTs them in ≤10k batches.

    .venv/bin/python submit_indexnow.py           # submit all
    .venv/bin/python submit_indexnow.py --dry-run # print counts only
"""
import sys

from app import db, helpers, indexnow
from app.config import get_settings
from app.routes.public import PUBLIC_STATUSES_SQL


def all_public_urls(conn) -> list[str]:
    base = get_settings().base_url
    urls = [f"{base}/", f"{base}/map", f"{base}/investigate", f"{base}/guide"]
    for r in conn.execute(
        f"SELECT id, title FROM sightings WHERE status IN {PUBLIC_STATUSES_SQL} "
        f"ORDER BY id"
    ):
        urls.append(f"{base}/sighting/{r['id']}/{helpers.slugify(r['title'])}")
    return urls


def main() -> None:
    dry = "--dry-run" in sys.argv
    s = get_settings()
    if not s.indexnow_key:
        raise SystemExit("INDEXNOW_KEY is not set — nothing to do.")
    conn = db.connect(s.db_path)
    urls = all_public_urls(conn)
    print(f"{len(urls)} URLs to submit "
          f"(key {s.indexnow_key[:6]}…, host {s.base_url})", flush=True)
    if dry:
        print("dry run — first 3:", urls[:3])
        return
    result = indexnow.submit_urls(urls)
    print(f"done: {result}", flush=True)


if __name__ == "__main__":
    main()
