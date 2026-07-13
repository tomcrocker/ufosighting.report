"""Repair sighting geo + sighting time from the mod team's curated Google
Sheet (r/UFOs Sighting Reports), exported as data/sightings_sheet.csv with
columns: Location, Date, Post Submission Time (UTC), Link.

Two gap types, filled independently and never overwriting real data:
- lat IS NULL and the sheet has a Location  -> geocode ladder
- sighted_at equals the post submission time (build_sighted_at's no-date
  fallback) and the sheet has a Date        -> LLM date/time extraction

The sheet's Date column is messy ("August 31, 2023 8:21 PM", "9:00 PM",
"October 17th/2017 @ 10PM"). We reuse the ingest extractor: it sees the post
title too, so a bare time still resolves when the title carries the date —
and when no date is recoverable anywhere we keep the submission-time fallback
rather than guess (people post days after the sighting).

    nohup .venv/bin/python csv_repair.py > /tmp/csv_repair.log 2>&1 &

Resume-safe: repaired/attempted sighting ids in data/csv_repair_done.json.
"""
import csv
import json
import os
import re
import sys
from datetime import datetime

import ingest
from app import db, extract, geocode, search
from app.config import get_settings

CSV_PATH = "data/sightings_sheet.csv"
STATE = "data/csv_repair_done.json"
LINK_RE = re.compile(r"/comments/([a-z0-9]{5,9})")
FALLBACK_SLOP_S = 120  # sighted_at this close to submission time = no-date fallback

# The extractor will invent a calendar date when handed a bare time ("23:21"
# produced 2023-10-01 in testing). Only accept a time fix when a real
# month+day appears in the sheet's Date field or the title — a year may be
# inferred, a date may not.
_MONTH_DAY = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*,?\s*"
    r"\d{1,2}(?:st|nd|rd|th)?\b"
    r"|\b\d{1,2}(?:st|nd|rd|th)?\s*(?:of\s+)?"
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", re.I)
_NUMERIC_DATE = re.compile(r"\b\d{1,4}\s*[/.-]\s*\d{1,2}\s*[/.-]\s*\d{2,4}\b")


def date_corroborated(*texts: str) -> bool:
    t = " ".join(x or "" for x in texts)
    return bool(_MONTH_DAY.search(t) or _NUMERIC_DATE.search(t))


def load_sheet(path: str) -> dict[str, dict]:
    out = {}
    for r in csv.DictReader(open(path)):
        m = LINK_RE.search(r.get("Link") or "")
        if m:
            out[m.group(1)] = {
                "location": (r.get("Location") or "").strip(),
                "date": (r.get("Date") or "").strip(),
                "submitted": (r.get("Post Submission Time") or "").strip(),
            }
    return out


def is_time_fallback(sighted_at: str, submitted: str) -> bool:
    try:
        sa = datetime.strptime(sighted_at, "%Y-%m-%dT%H:%M:%SZ")
        sub = datetime.strptime(submitted, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    return abs((sa - sub).total_seconds()) <= FALLBACK_SLOP_S


def repair_row(conn, row, sheet: dict) -> list[str]:
    """Returns the list of fixes applied ("geo", "time")."""
    need_geo = row["lat"] is None and sheet["location"]
    need_time = (sheet["date"]
                 and is_time_fallback(row["sighted_at"], sheet["submitted"])
                 and date_corroborated(sheet["date"], row["title"]))
    if not (need_geo or need_time):
        return []

    clamped = None
    if need_time or (need_geo and sheet["date"]):
        # the extractor resolves messy sheet dates and infers the IANA
        # timezone from the location; the title often supplies a missing date
        text = extract.combine_post_text(
            {"title": row["title"],
             "selftext": f"Location: {sheet['location']}\n"
                         f"Time and date: {sheet['date']}"}, [])
        clamped = extract.validate_and_clamp(
            extract.extract_fields(text), post_created_iso=row["sighted_at"])

    fixes = []
    if need_geo:
        coords = None
        queries = geocode.candidates(
            sheet["location"],
            clamped.get("city") if clamped else None,
            clamped.get("country") if clamped else None)
        for q in queries:
            coords = geocode.forward(conn, q)
            if coords:
                break
        if coords:
            conn.execute(
                """UPDATE sightings SET lat=?, lon=?, city=COALESCE(NULLIF(?,''), city),
                     country=COALESCE(NULLIF(?,''), country),
                     location_text=CASE WHEN TRIM(COALESCE(location_text,''))='' THEN ? ELSE location_text END
                   WHERE id=?""",
                (coords["lat"], coords["lon"], coords.get("city") or "",
                 coords.get("country") or "", sheet["location"][:300], row["id"]))
            fixes.append("geo")
    if need_time and clamped and clamped.get("date"):
        sighted_at, tz_name = ingest.build_sighted_at(clamped, row["sighted_at"])
        if sighted_at != row["sighted_at"]:
            conn.execute("UPDATE sightings SET sighted_at=?, tz_name=? WHERE id=?",
                         (sighted_at, tz_name, row["id"]))
            fixes.append("time")
    if fixes:
        # stale sky context once place or time moved; backfill_sky recomputes
        conn.execute("UPDATE sightings SET sky_events=NULL WHERE id=?", (row["id"],))
        conn.commit()
        search.index_sightings(conn, [row["id"]])
    return fixes


def main() -> None:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    conn = db.connect(get_settings().db_path)
    sheet = load_sheet(CSV_PATH)
    done = set(json.load(open(STATE))) if os.path.exists(STATE) else set()
    todo = []
    for pid, entry in sheet.items():
        row = conn.execute(
            """SELECT id, title, lat, sighted_at FROM sightings
               WHERE reddit_post_id=? AND status IN ('live','deleted_by_user','removed_on_reddit')""",
            (pid,)).fetchone()
        if row and row["id"] not in done:
            todo.append((row, entry))
    if limit:
        todo = todo[:limit]
    print(f"{len(sheet)} sheet rows, {len(todo)} matched sightings to check", flush=True)
    geo = tm = 0
    for n, (row, entry) in enumerate(todo, 1):
        try:
            fixes = repair_row(conn, row, entry)
            if fixes:
                geo += "geo" in fixes
                tm += "time" in fixes
                print(f"REPAIRED sighting {row['id']}: {'+'.join(fixes)} "
                      f"(loc={entry['location'][:60]!r} date={entry['date'][:40]!r})",
                      flush=True)
        except Exception as exc:
            print(f"sighting {row['id']} failed: {exc}", flush=True)
        done.add(row["id"])
        if n % 25 == 0:
            json.dump(sorted(done), open(STATE, "w"))
            print(f"progress: {n}/{len(todo)}, {geo} geo + {tm} time fixes", flush=True)
    json.dump(sorted(done), open(STATE, "w"))
    print(f"done: {geo} geo fixes, {tm} time fixes across {len(todo)} checked", flush=True)


if __name__ == "__main__":
    main()
