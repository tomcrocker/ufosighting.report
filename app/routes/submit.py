import hmac
import json
import re
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app import db, geocode, helpers, r2, ratelimit, reddit, turnstile, verify
from app.config import get_settings
from app.web import client_ip, new_csrf, templates

router = APIRouter()

KEY_RE = re.compile(r"^uploads/\d{4}/\d{2}/[0-9a-f]{32}\.[a-z0-9]{2,5}$")


class PresignRequest(BaseModel):
    filename: str
    content_type: str
    size_bytes: int


@router.post("/api/presign")
def presign(req: PresignRequest, request: Request, conn=Depends(db.get_db)):
    s = get_settings()
    ip = client_ip(request)
    if not ratelimit.allowed(conn, ip, "presign", s.rate_presign_per_hour):
        raise HTTPException(status_code=429, detail="Too many uploads — please try again later")
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
    ratelimit.record(conn, ip, "presign")
    return {
        "key": key,
        "upload_url": r2.presign_put(key, req.content_type, req.size_bytes),
        "public_url": r2.public_url(key),
        "kind": kind,
    }


_geocode_cache: dict[str, list] = {}


@router.get("/api/geocode")
def geocode_endpoint(q: str = "", request: Request = None, conn=Depends(db.get_db)):
    q = q.strip()
    if len(q) < 3:
        return {"results": []}
    s = get_settings()
    ip = client_ip(request)
    if not ratelimit.allowed(conn, ip, "geocode", s.rate_geocode_per_hour):
        raise HTTPException(status_code=429, detail="Too many lookups — try again later")
    cache_key = q.lower()
    if cache_key in _geocode_cache:
        return {"results": _geocode_cache[cache_key]}
    ratelimit.record(conn, ip, "geocode")
    try:
        results = geocode.search(q, limit=5)
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="Geocoder unavailable, drop a pin instead")
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


def _render_form(request, values, errors, *, csrf, status_code=200):
    s = get_settings()
    resp = templates.TemplateResponse(
        request,
        "submit.html",
        {
            "user": None,
            "values": values,
            "errors": errors,
            "csrf": csrf,
            "turnstile_site_key": s.turnstile_site_key,
            "max_files": s.max_files,
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
    resp.set_cookie("csrf", csrf, max_age=7200, httponly=True, samesite="lax")
    return resp


@router.get("/submit")
def submit_form(request: Request):
    csrf = request.cookies.get("csrf") or new_csrf()
    return _render_form(request, values={}, errors=[], csrf=csrf)


def _try_send_verify_dm(conn, username: str, token: str) -> None:
    """Fire the verification DM; guarded per-username, non-fatal on failure."""
    s = get_settings()
    if not ratelimit.allowed(conn, username.lower(), "dm", 1,
                             window_hours=s.verify_dm_per_username_hours):
        return
    verify_url = f"{s.base_url}/verify/{token}"
    subject, text = verify.verify_message(username, verify_url)
    try:
        reddit.send_message(reddit.script_token(), to=username, subject=subject, text=text)
        ratelimit.record(conn, username.lower(), "dm")
    except reddit.RedditError as exc:
        print(f"verify DM to u/{username} failed: {exc}")


@router.post("/submit")
async def submit_create(request: Request, conn=Depends(db.get_db)):
    s = get_settings()
    ip = client_ip(request)
    form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}

    cookie_csrf = request.cookies.get("csrf", "")
    if not cookie_csrf or not hmac.compare_digest(form.get("csrf_token", ""), cookie_csrf):
        raise HTTPException(status_code=403, detail="Bad CSRF token")

    if not turnstile.verify(form.get("cf-turnstile-response", ""), ip):
        return _render_form(request, form, ["Anti-spam check failed — please try again."],
                            csrf=cookie_csrf, status_code=400)

    if not ratelimit.allowed(conn, ip, "submit", s.rate_submit_per_hour):
        return _render_form(request, form,
                            ["You've submitted several sightings recently. Please try again later."],
                            csrf=cookie_csrf, status_code=429)

    username = helpers.clean_username(form.get("reddit_username"))
    clean, errors = validate_submission(form)
    if username is None:
        errors.insert(0, "Enter a valid Reddit username (3–20 letters, digits, _ or -).")
    for m in clean["media"]:
        if not r2.head_exists(m["key"]):
            errors.append("An uploaded file was not found in storage — please re-upload.")
            break
    if errors:
        return _render_form(request, form, errors, csrf=cookie_csrf, status_code=422)

    token = verify.new_token()
    cur = conn.execute(
        """INSERT INTO sightings
             (reddit_username, title, description, sighted_at, tz_name, duration_seconds,
              shape, witnesses, num_objects, distance, apparent_size, movement,
              has_wings, has_rotors, has_plume, makes_noise, sensors, witness_background,
              location_text, city, country, lat, lon, location_obscured,
              submitter_ip, verify_token, verify_sent_at, status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                   strftime('%Y-%m-%dT%H:%M:%SZ','now'),'pending_verify')""",
        (
            username, clean["title"], clean["description"], clean["sighted_at"],
            clean["tz_name"], clean["duration_seconds"], clean["shape"], clean["witnesses"],
            clean["num_objects"], clean["distance"], clean["apparent_size"],
            json.dumps(clean["movement"]) if clean["movement"] else None,
            clean["has_wings"], clean["has_rotors"], clean["has_plume"], clean["makes_noise"],
            json.dumps(clean["sensors"]) if clean["sensors"] else None,
            json.dumps(clean["witness_background"]) if clean["witness_background"] else None,
            clean["location_text"], clean["city"], clean["country"],
            clean["lat"], clean["lon"], clean["location_obscured"], ip, token,
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
    ratelimit.record(conn, ip, "submit")
    _try_send_verify_dm(conn, username, token)
    return templates.TemplateResponse(request, "submitted.html", {"user": None, "username": username})
