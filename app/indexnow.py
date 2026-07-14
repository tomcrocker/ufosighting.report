"""IndexNow (indexnow.org): ping search engines the moment a URL appears or
changes, instead of waiting for a crawl. One POST reaches Bing, Yandex,
Seznam, Naver and the rest of the shared network (Google does not participate,
but it costs nothing to notify the others).

Verification: the engines fetch https://<host>/<key>.txt and check it contains
the key (served by the /{key}.txt route in routes/public.py).

Everything here is best-effort — a failed submission must never break ingest
or the verify flow. Disabled cleanly when INDEXNOW_KEY is unset.
"""
from urllib.parse import urlparse

import httpx

from app.config import get_settings

ENDPOINT = "https://api.indexnow.org/indexnow"
BATCH = 10000  # IndexNow's documented per-request cap


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def submit_urls(urls: list[str], *, timeout: float = 60) -> dict:
    """POST absolute URLs to IndexNow in ≤10k batches. Returns a summary;
    never raises."""
    s = get_settings()
    urls = [u for u in dict.fromkeys(urls) if u]  # dedupe, keep order
    if not s.indexnow_key or not urls:
        return {"submitted": 0, "batches": 0, "skipped": not s.indexnow_key}
    host = urlparse(s.base_url).netloc
    key_location = f"{s.base_url}/{s.indexnow_key}.txt"
    submitted = batches = 0
    statuses = []
    for batch in _chunks(urls, BATCH):
        payload = {"host": host, "key": s.indexnow_key,
                   "keyLocation": key_location, "urlList": batch}
        try:
            resp = httpx.post(ENDPOINT, json=payload, timeout=timeout)
            statuses.append(resp.status_code)
            # 200 = accepted, 202 = accepted/queued; both are success
            if resp.status_code in (200, 202):
                submitted += len(batch)
            batches += 1
        except httpx.HTTPError as exc:
            statuses.append(f"error: {exc}")
    return {"submitted": submitted, "batches": batches, "statuses": statuses}


def submit_url(url: str) -> dict:
    """Single-URL convenience for the live paths (verify go-live, daily ingest)."""
    return submit_urls([url])
