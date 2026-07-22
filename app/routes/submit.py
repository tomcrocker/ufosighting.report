import hmac
import json
import re
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app import (db, geocode, helpers, mediameta, orphans, r2, ratelimit, reddit,
                 turnstile, verify)
from app.countries import COUNTRY_NAMES
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
    # the PUT goes browser->R2 directly, so this row is the only record that
    # this file was ever offered an upload slot (see app/orphans.py)
    orphans.record_key(conn, key=key, ip=ip, kind=kind)
    return {
        "key": key,
        "upload_url": r2.presign_put(key, req.content_type, req.size_bytes),
        "public_url": r2.public_url(key),
        "kind": kind,
    }


_geocode_cache: dict[str, list] = {}

# Whole countries/continents are useless as sighting locations (guideline:
# within ~20 km) — never offer them in the autocomplete.
_TOO_BROAD = {"country", "continent", "ocean", "sea"}


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
        results = geocode.suggest(q, limit=6)
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="Geocoder unavailable, drop a pin instead")
    results = [r for r in results if r.get("addresstype") not in _TOO_BROAD]
    if len(_geocode_cache) < 5000:
        _geocode_cache[cache_key] = results
    return {"results": results}


class MediaMetaRequest(BaseModel):
    key: str
    kind: str


@router.post("/api/media-meta")
def media_meta(req: MediaMetaRequest, request: Request, conn=Depends(db.get_db)):
    """Preview the technical metadata found in a just-uploaded file so the
    reporter can decide what to publish (device / time / location)."""
    if not KEY_RE.fullmatch(req.key) or req.kind not in ("image", "video"):
        raise HTTPException(status_code=400, detail="Invalid media reference")
    s = get_settings()
    ip = client_ip(request)
    if not ratelimit.allowed(conn, ip, "presign", s.rate_presign_per_hour):
        raise HTTPException(status_code=429, detail="Too many requests")
    url = r2.public_url(req.key)
    if req.kind == "image":
        try:
            resp = httpx.get(url, timeout=30)
            meta = mediameta.extract_image_meta(resp.content) if resp.status_code == 200 else {}
        except httpx.HTTPError:
            meta = {}
    else:
        meta = mediameta.extract_video_meta(url)
    return {
        "rows": mediameta.public_rows(meta, include_gps=True),
        "provenance": mediameta.provenance(meta),
        "has": {
            "device": any(meta.get(k) for k in ("make", "model", "lens", "software", "encoder")),
            "time": bool(meta.get("captured_at")),
            "location": meta.get("gps_lat") is not None,
        },
    }


@router.get("/api/reverse")
def reverse_endpoint(lat: float, lon: float, request: Request = None,
                     conn=Depends(db.get_db)):
    """Nearest town/city for a dropped pin (shares the geocode rate bucket)."""
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise HTTPException(status_code=400, detail="Invalid coordinates")
    s = get_settings()
    ip = client_ip(request)
    if not ratelimit.allowed(conn, ip, "geocode", s.rate_geocode_per_hour):
        raise HTTPException(status_code=429, detail="Too many lookups — try again later")
    ratelimit.record(conn, ip, "geocode")
    out = geocode.reverse(lat, lon)
    if out is None:
        raise HTTPException(status_code=502, detail="Geocoder unavailable")
    return {"label": out["label"], "city": out["city"], "country": out["country"]}


def _custom_option(value: str | None) -> str | None:
    """Free-text 'other' shape/movement: printable words only, capped."""
    cleaned = re.sub(r"[^A-Za-z0-9 ,'()\-]", "", (value or "")).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)[:40].strip().lower()
    return cleaned or None


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
    if not 15 <= len(clean["title"]) <= 300:
        errors.append("Title must be 15-300 characters — make it descriptive, "
                      "it becomes the Reddit post title.")

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

    # "other" chip → free-text shape/movement, sanitized and length-capped
    if form.get("shape") == "other":
        clean["shape"] = _custom_option(form.get("shape_other"))
        if not clean["shape"]:
            errors.append("Describe the custom shape (or pick one from the list).")
    else:
        clean["shape"] = _clean_choice(form.get("shape"), helpers.SHAPES)
        if not clean["shape"]:
            errors.append("Select the object's shape.")
    clean["num_objects"] = _clean_choice(form.get("num_objects"), helpers.NUM_OBJECTS)
    if not clean["num_objects"]:
        errors.append("Select how many objects you saw.")
    clean["distance"] = _clean_choice(form.get("distance"), helpers.DISTANCES)
    clean["apparent_size"] = _clean_choice(form.get("apparent_size"), helpers.SIZES)
    clean["movement"] = _clean_multi(form.get("movement_json"), helpers.MOVEMENTS)
    try:
        raw_movement = json.loads(form.get("movement_json") or "[]")
    except ValueError:
        raw_movement = []
    if isinstance(raw_movement, list) and "other" in raw_movement:
        custom = _custom_option(form.get("movement_other"))
        if custom:
            clean["movement"].append(custom)
        else:
            errors.append("Describe the custom movement (or unselect 'other').")
    if not clean["movement"]:
        errors.append("Select at least one movement pattern.")
    clean["sensors"] = _clean_multi(form.get("sensors_json"), helpers.SENSOR_OPTIONS)
    clean["witness_background"] = _clean_multi(
        form.get("background_json"), helpers.BACKGROUND_OPTIONS
    )
    missing_features = missing_obs = False
    for field in ("has_wings", "has_rotors", "has_plume", "makes_noise"):
        clean[field] = _clean_choice(form.get(field), helpers.FEATURE_ANSWERS)
        missing_features = missing_features or clean[field] is None
    for key, _q, _hint in helpers.OBSERVABLES:
        clean[key] = _clean_choice(form.get(key), helpers.FEATURE_ANSWERS)
        missing_obs = missing_obs or clean[key] is None
    if missing_features:
        errors.append("Answer the wings/rotors/plume/noise questions — "
                      "'Not sure' is a valid answer.")
    if missing_obs:
        errors.append("Answer the five observables — 'Not sure' is a valid answer.")

    # r/UFOs guideline gates: a rule-out statement is always required; the
    # camera confirmations only when media is attached (checked below).
    clean["rule_out"] = (form.get("rule_out") or "").strip()
    if len(clean["rule_out"]) < 20:
        errors.append(
            "Briefly rule out common explanations (aircraft, drone, Starlink, "
            "planet, balloon…) — one sentence of at least 20 characters."
        )
    # first-hand (own sighting) vs second-hand (sharing someone else's). A shared
    # report waives the eyewitness + capture confirmations but must name a source.
    clean["first_hand"] = 0 if form.get("is_shared") in ("1", "on", "true") else 1
    clean["source_note"] = (form.get("source_note") or "").strip()[:500] or None
    if clean["first_hand"] == 0:
        if not clean["source_note"]:
            errors.append("Add where this shared sighting is from (a group, person, or link).")
    else:
        clean["source_note"] = None
        if form.get("confirm_eyewitness") not in ("1", "on", "true"):
            errors.append("Confirm you saw this with your own eyes at the time.")

    clean["capture_device"] = (form.get("capture_device") or "").strip()[:100] or None

    clean["location_text"] = (form.get("location_text") or "").strip()
    coord_m = re.fullmatch(
        r"(-?\d{1,3}(?:\.\d+)?)\s*[, ]\s*(-?\d{1,3}(?:\.\d+)?)",
        clean["location_text"].replace("°", ""))
    if len(clean["location_text"]) < 2:
        errors.append("Enter a location.")
    elif coord_m:
        # bare coordinates are a valid (and precise) location
        try:
            c_lat, c_lon = float(coord_m.group(1)), float(coord_m.group(2))
            if not (-90 <= c_lat <= 90 and -180 <= c_lon <= 180):
                raise ValueError
            if not (form.get("lat") or "").strip():
                form = dict(form)
                form["lat"], form["lon"] = str(c_lat), str(c_lon)
        except ValueError:
            errors.append("Those coordinates don't look valid (lat, lon).")
    elif clean["location_text"].lower().rstrip(".") in COUNTRY_NAMES:
        errors.append(
            "A whole country isn't precise enough to investigate — name the "
            "town or area (within ~20 km is ideal)."
        )
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
        prefs = item.get("exif") or {}
        clean["media"].append(
            {
                "key": key,
                "kind": kind,
                "width": item.get("width"),
                "height": item.get("height"),
                "size_bytes": item.get("size_bytes"),
                # per-file metadata consent — defaults to publish everything
                "exif_prefs": {
                    "device": bool(prefs.get("device", True)),
                    "time": bool(prefs.get("time", True)),
                    "location": bool(prefs.get("location", True)),
                },
            }
        )
    if clean["media"] and clean["first_hand"] == 1:  # shared reports skip capture confirms
        confirms = (
            ("confirm_own_capture",
             "Confirm you took this photo/video yourself and saw it with "
             "your own eyes."),
            ("confirm_no_fixed_cam",
             "Confirm this isn't trail-camera or doorbell-camera footage."),
            ("confirm_not_screen",
             "Confirm this isn't a recording of another screen or a repost "
             "(TV, TikTok, screenshots)."),
            ("confirm_in_focus",
             "Confirm the imagery is in focus most of the time."),
        )
        for field, msg in confirms:
            if form.get(field) not in ("1", "on", "true"):
                errors.append(msg)
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
                "observables": helpers.OBSERVABLES,
                "shapes": helpers.SHAPES,
                "num_objects": helpers.NUM_OBJECTS,
                "distances": helpers.DISTANCES,
                "sizes": helpers.SIZES,
                "movements": helpers.MOVEMENTS,
                "sensors": helpers.SENSOR_OPTIONS,
                "backgrounds": helpers.BACKGROUND_OPTIONS,
            },
            "show_all": bool(errors),
            "canonical": f"{s.base_url}/submit",
        },
        status_code=status_code,
    )
    resp.set_cookie("csrf", csrf, max_age=7200, httponly=True, samesite="lax")
    return resp


@router.get("/submit")
def submit_form(request: Request):
    csrf = request.cookies.get("csrf") or new_csrf()
    return _render_form(request, values={}, errors=[], csrf=csrf)


def _try_send_verify_dm(conn, username: str, token: str) -> str:
    """Fire the verification DM; guarded per-username, non-fatal on failure.
    Returns the outcome so the confirmation page can tell the user the truth:
    'sent' | 'throttled' (recent DM still within the gate) | 'failed'."""
    s = get_settings()
    if not ratelimit.allowed(conn, username.lower(), "dm", 1,
                             window_hours=s.verify_dm_per_username_hours):
        return "throttled"
    verify_url = f"{s.base_url}/verify/{token}"
    subject, text = verify.verify_message(username, verify_url)
    try:
        reddit.send_message(reddit.script_token(), to=username, subject=subject, text=text)
        ratelimit.record(conn, username.lower(), "dm")
        return "sent"
    except reddit.RedditError as exc:
        print(f"verify DM to u/{username} failed: {exc}")
        return "failed"


def _render_submitted(request, conn, username, dm_status, token, *, resent=False):
    """Confirmation page, honest about what actually happened with the DM."""
    s = get_settings()
    retry_minutes = (
        ratelimit.retry_after_minutes(conn, username.lower(), "dm",
                                      s.verify_dm_per_username_hours)
        if dm_status == "throttled" else 0)
    # Reuse the existing csrf cookie (both callers arrive with a valid one) so the
    # resend form matches it without rotating the token mid-session.
    csrf = request.cookies.get("csrf") or new_csrf()
    resp = templates.TemplateResponse(request, "submitted.html", {
        "user": None, "username": username, "bot_username": s.script_username,
        "verify_hours": s.verify_window_hours, "dm_status": dm_status,
        "retry_minutes": retry_minutes, "verify_token": token, "csrf": csrf,
        "resent": resent})
    if not request.cookies.get("csrf"):
        resp.set_cookie("csrf", csrf, max_age=7200, httponly=True, samesite="lax")
    return resp


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
              location_text, city, country, lat, lon, location_obscured, rule_out,
              capture_device, obs_accel, obs_no_signature, obs_low_observability,
              obs_transmedium, obs_positive_lift, first_hand, source_note,
              submitter_ip, verify_token, verify_sent_at, status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
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
            clean["lat"], clean["lon"], clean["location_obscured"], clean["rule_out"],
            clean["capture_device"], clean["obs_accel"], clean["obs_no_signature"],
            clean["obs_low_observability"], clean["obs_transmedium"],
            clean["obs_positive_lift"], clean["first_hand"], clean["source_note"],
            ip, token,
        ),
    )
    sighting_id = cur.lastrowid
    for i, m in enumerate(clean["media"]):
        conn.execute(
            """INSERT INTO media (sighting_id, r2_key, kind, width, height, size_bytes,
                                  sort_order, exif_prefs)
               VALUES (?,?,?,?,?,?,?,?)""",
            (sighting_id, m["key"], m["kind"], m["width"], m["height"], m["size_bytes"],
             i, json.dumps(m["exif_prefs"])),
        )
    conn.commit()
    ratelimit.record(conn, ip, "submit")
    # catch a file that was uploaded but dropped before submit, while we still
    # know which reporter and sighting it belonged to
    orphans.warn_for_submission(
        conn, ip=ip, attached=[m["key"] for m in clean["media"]], sighting_id=sighting_id)
    dm_status = _try_send_verify_dm(conn, username, token)
    return _render_submitted(request, conn, username, dm_status, token)


@router.post("/submit/resend")
async def submit_resend(request: Request, conn=Depends(db.get_db)):
    """Re-fire the verification DM for a still-pending sighting, respecting the
    per-username gate. Reached from the 'resend' button on the confirmation page."""
    form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    cookie_csrf = request.cookies.get("csrf", "")
    if not cookie_csrf or not hmac.compare_digest(form.get("csrf_token", ""), cookie_csrf):
        raise HTTPException(status_code=403, detail="Bad CSRF token")
    token = form.get("token", "")
    row = conn.execute(
        "SELECT reddit_username FROM sightings "
        "WHERE verify_token=? AND status='pending_verify'", (token,)).fetchone()
    if not row:  # already verified, expired, or bad token
        return _render_submitted(request, conn, "", "gone", token)
    username = row["reddit_username"]
    dm_status = _try_send_verify_dm(conn, username, token)
    return _render_submitted(request, conn, username, dm_status, token, resent=True)
