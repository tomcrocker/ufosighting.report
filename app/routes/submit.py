import hmac
import json
import re
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app import auth, db, helpers, r2, reddit
from app.config import get_settings
from app.web import current_user, templates

router = APIRouter()

KEY_RE = re.compile(r"^uploads/\d{4}/\d{2}/[0-9a-f]{32}\.[a-z0-9]{2,5}$")


class PresignRequest(BaseModel):
    filename: str
    content_type: str
    size_bytes: int


@router.post("/api/presign")
def presign(req: PresignRequest, user=Depends(current_user)):
    if user is None:
        raise HTTPException(status_code=401, detail="Log in with Reddit first")
    s = get_settings()
    if req.content_type in r2.ALLOWED_IMAGE:
        kind, cap = "image", s.max_image_bytes
    elif req.content_type in r2.ALLOWED_VIDEO:
        kind, cap = "video", s.max_video_bytes
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {req.content_type}")
    if not 0 < req.size_bytes <= cap:
        raise HTTPException(
            status_code=400,
            detail=f"File too large — max {cap // (1024 * 1024)}MB for {kind}s",
        )
    key = r2.make_upload_key(req.content_type)
    return {
        "key": key,
        "upload_url": r2.presign_put(key, req.content_type, req.size_bytes),
        "public_url": r2.public_url(key),
        "kind": kind,
    }


GEOCODE_URL = "https://nominatim.openstreetmap.org/search"
_geocode_cache: dict[str, list] = {}


@router.get("/api/geocode")
def geocode(q: str = "", user=Depends(current_user)):
    if user is None:
        raise HTTPException(status_code=401, detail="Log in with Reddit first")
    q = q.strip()
    if len(q) < 3:
        return {"results": []}
    cache_key = q.lower()
    if cache_key in _geocode_cache:
        return {"results": _geocode_cache[cache_key]}
    resp = httpx.get(
        GEOCODE_URL,
        params={"q": q, "format": "jsonv2", "limit": 5, "addressdetails": 1},
        headers={"User-Agent": get_settings().user_agent},
        timeout=10,
    )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Geocoder unavailable, drop a pin instead")
    results = []
    for item in resp.json():
        addr = item.get("address", {})
        results.append(
            {
                "display_name": item.get("display_name", ""),
                "lat": float(item["lat"]),
                "lon": float(item["lon"]),
                "city": addr.get("city") or addr.get("town") or addr.get("village")
                or addr.get("municipality") or "",
                "country": addr.get("country", ""),
            }
        )
    if len(_geocode_cache) < 5000:
        _geocode_cache[cache_key] = results
    return {"results": results}


def _clean_choice(value: str | None, options: list[str]) -> str | None:
    value = (value or "").strip()
    return value if value in options else None


def _clean_multi(raw_json: str | None, options: list[str]) -> list[str]:
    try:
        items = json.loads(raw_json or "[]")
    except ValueError:
        return []
    if not isinstance(items, list):
        return []
    picked: list[str] = []
    for item in items:
        if isinstance(item, str) and item in options and item not in picked:
            picked.append(item)
    return picked


def validate_submission(form: dict) -> tuple[dict, list[str]]:
    errors: list[str] = []
    clean: dict = {}

    clean["title"] = (form.get("title") or "").strip()
    if not 5 <= len(clean["title"]) <= 300:
        errors.append("Title must be 5-300 characters.")

    clean["description"] = (form.get("description") or "").strip()
    if len(clean["description"]) < helpers.MIN_STORY_CHARS:
        errors.append(
            f"Your story must be at least {helpers.MIN_STORY_CHARS} characters — the details matter."
        )

    tz_name = (form.get("tz_name") or "UTC").strip()
    try:
        ZoneInfo(tz_name)
    except Exception:
        errors.append("Unknown timezone.")
        tz_name = "UTC"
    clean["tz_name"] = tz_name

    try:
        clean["sighted_at"] = helpers.to_utc(
            form.get("sighted_date", ""), form.get("sighted_time", ""), tz_name
        ).strftime(helpers.ISO)
    except (ValueError, TypeError):
        errors.append("Enter a valid date and time.")
        clean["sighted_at"] = None

    clean["duration_seconds"] = None
    if (form.get("duration_value") or "").strip():
        try:
            value = float(form["duration_value"])
            unit = form.get("duration_unit", "seconds")
            factor = {"seconds": 1, "minutes": 60, "hours": 3600}[unit]
            seconds = int(value * factor)
            if not 1 <= seconds <= 86400:
                raise ValueError
            clean["duration_seconds"] = seconds
        except (ValueError, KeyError):
            errors.append("Enter a valid duration.")

    clean["witnesses"] = None
    if (form.get("witnesses") or "").strip():
        try:
            witnesses = int(form["witnesses"])
            if not 1 <= witnesses <= 1000:
                raise ValueError
            clean["witnesses"] = witnesses
        except ValueError:
            errors.append("Enter a valid witness count.")

    clean["shape"] = _clean_choice(form.get("shape"), helpers.SHAPES)
    clean["num_objects"] = _clean_choice(form.get("num_objects"), helpers.NUM_OBJECTS)
    clean["distance"] = _clean_choice(form.get("distance"), helpers.DISTANCES)
    clean["apparent_size"] = _clean_choice(form.get("apparent_size"), helpers.SIZES)
    clean["movement"] = _clean_multi(form.get("movement_json"), helpers.MOVEMENTS)
    clean["sensors"] = _clean_multi(form.get("sensors_json"), helpers.SENSOR_OPTIONS)
    clean["witness_background"] = _clean_multi(
        form.get("background_json"), helpers.BACKGROUND_OPTIONS
    )
    for field in ("has_wings", "has_rotors", "has_plume", "makes_noise"):
        clean[field] = _clean_choice(form.get(field), helpers.FEATURE_ANSWERS)

    clean["location_text"] = (form.get("location_text") or "").strip()
    if len(clean["location_text"]) < 2:
        errors.append("Enter a location.")
    clean["city"] = (form.get("city") or "").strip() or None
    clean["country"] = (form.get("country") or "").strip() or None

    clean["lat"], clean["lon"] = None, None
    lat_raw, lon_raw = (form.get("lat") or "").strip(), (form.get("lon") or "").strip()
    if lat_raw or lon_raw:
        try:
            lat, lon = float(lat_raw), float(lon_raw)
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError
            clean["lat"], clean["lon"] = lat, lon
        except ValueError:
            errors.append("Map pin coordinates are invalid.")

    clean["location_obscured"] = 1 if form.get("location_obscured") in ("1", "on", "true") else 0
    if clean["location_obscured"]:
        if clean["lat"] is not None:
            clean["lat"] = round(clean["lat"], 1)
            clean["lon"] = round(clean["lon"], 1)
        obscured_parts = [p for p in (clean["city"], clean["country"]) if p]
        if obscured_parts:
            clean["location_text"] = ", ".join(obscured_parts)

    clean["media"] = []
    raw = form.get("media_json") or "[]"
    try:
        items = json.loads(raw)
        assert isinstance(items, list)
    except (ValueError, AssertionError):
        errors.append("Media list is corrupted — please re-upload.")
        items = []
    if len(items) > get_settings().max_files:
        errors.append(f"At most {get_settings().max_files} files per sighting.")
        items = []
    for item in items:
        key, kind = str(item.get("key", "")), str(item.get("kind", ""))
        if not KEY_RE.fullmatch(key) or kind not in ("image", "video"):
            errors.append("An uploaded file reference is invalid — please re-upload.")
            break
        clean["media"].append(
            {
                "key": key,
                "kind": kind,
                "width": item.get("width"),
                "height": item.get("height"),
                "size_bytes": item.get("size_bytes"),
            }
        )
    return clean, errors


def _render_form(request, user, values, errors, status_code=200):
    return templates.TemplateResponse(
        request,
        "submit.html",
        {
            "user": user,
            "values": values,
            "errors": errors,
            "csrf_token": auth.csrf_for(user.id),
            "max_files": get_settings().max_files,
            "opts": {
                "shapes": helpers.SHAPES,
                "num_objects": helpers.NUM_OBJECTS,
                "distances": helpers.DISTANCES,
                "sizes": helpers.SIZES,
                "movements": helpers.MOVEMENTS,
                "sensors": helpers.SENSOR_OPTIONS,
                "backgrounds": helpers.BACKGROUND_OPTIONS,
            },
            "show_all": bool(errors),
        },
        status_code=status_code,
    )


@router.get("/submit")
def submit_form(request: Request, conn=Depends(db.get_db), user=Depends(current_user)):
    if user is None:
        return templates.TemplateResponse(
            request, "login.html", {"user": None, "next_url": "/submit"}
        )
    values = auth.load_draft(conn, user.username) or {}
    return _render_form(request, user, values, errors=[])


@router.post("/submit")
async def submit_create(request: Request, conn=Depends(db.get_db), user=Depends(current_user)):
    if user is None:
        return RedirectResponse("/auth/login?next=/submit", status_code=303)
    form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    if not hmac.compare_digest(form.get("csrf_token", ""), auth.csrf_for(user.id)):
        raise HTTPException(status_code=403, detail="Bad CSRF token")

    clean, errors = validate_submission(form)
    for m in clean["media"]:
        if not r2.head_exists(m["key"]):
            errors.append("An uploaded file was not found in storage — please re-upload.")
            break
    if errors:
        return _render_form(request, user, form, errors, status_code=422)

    s = get_settings()
    cur = conn.execute(
        """INSERT INTO sightings
             (reddit_username, title, description, sighted_at, tz_name, duration_seconds,
              shape, witnesses, num_objects, distance, apparent_size, movement,
              has_wings, has_rotors, has_plume, makes_noise, sensors, witness_background,
              location_text, city, country, lat, lon, location_obscured, status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending_post')""",
        (
            user.username, clean["title"], clean["description"], clean["sighted_at"],
            clean["tz_name"], clean["duration_seconds"], clean["shape"], clean["witnesses"],
            clean["num_objects"], clean["distance"], clean["apparent_size"],
            json.dumps(clean["movement"]) if clean["movement"] else None,
            clean["has_wings"], clean["has_rotors"], clean["has_plume"], clean["makes_noise"],
            json.dumps(clean["sensors"]) if clean["sensors"] else None,
            json.dumps(clean["witness_background"]) if clean["witness_background"] else None,
            clean["location_text"], clean["city"], clean["country"],
            clean["lat"], clean["lon"], clean["location_obscured"],
        ),
    )
    sighting_id = cur.lastrowid
    for i, m in enumerate(clean["media"]):
        conn.execute(
            """INSERT INTO media (sighting_id, r2_key, kind, width, height, size_bytes, sort_order)
               VALUES (?,?,?,?,?,?,?)""",
            (sighting_id, m["key"], m["kind"], m["width"], m["height"], m["size_bytes"], i),
        )
    conn.commit()

    def rollback():
        conn.execute("DELETE FROM sightings WHERE id=?", (sighting_id,))
        conn.commit()

    slug = helpers.slugify(clean["title"])
    gallery_url = f"{s.base_url}/sighting/{sighting_id}/{slug}"
    location_line = ", ".join(
        dict.fromkeys(p for p in (clean["location_text"], clean["city"], clean["country"]) if p)
    )
    body = helpers.format_post_body(
        clean,
        sighted_local=helpers.from_utc(clean["sighted_at"], clean["tz_name"]),
        location_line=location_line,
        media_urls=[r2.public_url(m["key"]) for m in clean["media"]],
        gallery_url=gallery_url,
    )
    try:
        post_id = reddit.submit_post(
            user.access_token,
            subreddit=s.subreddit,
            title=clean["title"],
            body=body,
            flair_id=s.sighting_flair_id,
        )
    except reddit.TokenExpired:
        auth.save_draft(conn, user.username, form)
        rollback()
        return RedirectResponse("/auth/login?next=/submit", status_code=303)
    except reddit.RateLimited as exc:
        rollback()
        return _render_form(request, user, form, [f"Reddit rate limit: {exc}"], status_code=429)
    except reddit.RedditError as exc:
        rollback()
        return _render_form(
            request, user, form, [f"Posting to Reddit failed: {exc}"], status_code=502
        )

    conn.execute(
        "UPDATE sightings SET reddit_post_id=?, status='live' WHERE id=?", (post_id, sighting_id)
    )
    conn.commit()
    auth.delete_draft(conn, user.username)
    return RedirectResponse(f"/sighting/{sighting_id}/{slug}", status_code=303)
