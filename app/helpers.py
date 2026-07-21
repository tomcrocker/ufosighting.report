import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ISO = "%Y-%m-%dT%H:%M:%SZ"

SHAPES = [
    "changing", "chevron", "cigar", "circle", "cone", "cross", "cube", "cylinder",
    "diamond", "disk", "egg", "fireball", "flash", "formation", "light", "oval",
    "rectangle", "saucer", "sphere", "teardrop", "triangle", "unknown",
]
NUM_OBJECTS = ["1", "2", "3", "4", "5+"]
DISTANCES = [
    "very close (under 50 ft)", "within a football field", "a few miles",
    "as far as the horizon", "above the trees", "as high as a plane", "as high as a star",
]
SIZES = [
    "pinhead", "pea", "dime", "quarter", "golf ball", "baseball", "grapefruit",
    "basketball", "larger",
]
MOVEMENTS = [
    "hovering", "floating around", "straight and steady", "circular",
    "slowly descending", "unpredictable, erratic", "random, smooth",
    "extremely fast", "abrupt changes in direction",
]
SENSOR_OPTIONS = ["infrared", "night vision", "radar", "sonar", "other"]
BACKGROUND_OPTIONS = ["active duty military", "veteran", "pilot", "scientist", "law enforcement"]
FEATURE_ANSWERS = ["yes", "no", "unsure"]

# The "five observables" (Elizondo/AATIP), shortened for the wizard
OBSERVABLES = [
    ("obs_accel", "Sudden, extreme acceleration?",
     "Instant maneuvers or direction reversals beyond any known aircraft."),
    ("obs_no_signature", "Extreme speed with no signatures?",
     "Hypersonic-looking motion but no sonic boom, contrail, or engine noise."),
    ("obs_low_observability", "Hard to observe clearly?",
     "Blurry or glowing edges, hard to focus on — by eye or camera."),
    ("obs_transmedium", "Moved between air, water, or space?",
     "Crossed between mediums without slowing or changing behavior."),
    ("obs_positive_lift", "Flight without visible means of lift?",
     "Hovered or flew with no wings, rotors, or exhaust."),
]
MIN_STORY_CHARS = 150

USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,20}$")


def clean_username(raw: str | None) -> str | None:
    if not raw:
        return None
    name = raw.strip()
    for prefix in ("/u/", "u/", "/U/", "U/"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    return name if USERNAME_RE.fullmatch(name) else None


def slugify(text: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "sighting"


def humanize_duration(seconds: int | None) -> str:
    if not seconds:
        return ""
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    if seconds < 3600:
        minutes = round(seconds / 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours = seconds / 3600
    hours_str = f"{hours:.1f}".rstrip("0").rstrip(".")
    return f"{hours_str} hour{'s' if hours != 1 else ''}"


def page_description(s) -> str:
    """Meta/JSON-LD description with a synthesized fallback — media-only
    posts have no body text, and an empty description field is both a GSC
    warning and a wasted SERP snippet."""
    desc = (s["description"] or "").strip()
    if desc:
        return desc[:300]
    shape = (s["shape"] or "").strip()
    loc = ", ".join(x for x in (s["city"], s["country"]) if x) \
        or (s["location_text"] or "").strip()
    out = "Eyewitness UFO report"
    if shape:
        out += f" of a {shape}-shaped object"
    if loc:
        out += f" over {loc}"
    out += f" on {s['sighted_at'][:10]}."
    out += " Archived with original-quality media for analysis."
    return out


_WINDS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
          "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def compass_name(deg: float) -> str:
    return _WINDS[round((deg % 360) / 22.5) % 16]


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    from math import asin, cos, radians, sin, sqrt
    rlat1, rlon1, rlat2, rlon2 = map(radians, (lat1, lon1, lat2, lon2))
    a = (sin((rlat2 - rlat1) / 2) ** 2
         + cos(rlat1) * cos(rlat2) * sin((rlon2 - rlon1) / 2) ** 2)
    return 6371.0 * 2 * asin(sqrt(a))


def to_utc(date_str: str, time_str: str, tz_name: str) -> datetime:
    local = datetime.fromisoformat(f"{date_str}T{time_str}").replace(tzinfo=ZoneInfo(tz_name))
    return local.astimezone(timezone.utc)


def from_utc(utc_str: str, tz_name: str) -> str:
    dt = datetime.strptime(utc_str, ISO).replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M")


def iso_from_epoch(epoch) -> str | None:
    """Reddit created_utc (unix seconds) -> our stored ISO-UTC string, or None
    for missing/zero/invalid input. Shared by ingest, the posting flow, and the
    backfill so reddit_posted_at has one canonical shape."""
    if not epoch:
        return None
    try:
        return datetime.fromtimestamp(int(epoch), timezone.utc).strftime(ISO)
    except (ValueError, OSError, OverflowError):
        return None


def post_date(iso: str | None) -> str:
    """Date-only (YYYY-MM-DD) for a stored ISO-UTC timestamp such as a Reddit
    post time. Returns '' for missing/malformed input so the caller can drop
    the row instead of rendering an empty cell. ISO format on purpose — the
    community asked for one consistent, unambiguous date mask everywhere
    (cards, When, filters all use it)."""
    if not iso:
        return ""
    try:
        datetime.strptime(iso, ISO)
    except (ValueError, TypeError):
        return ""
    return iso[:10]


def format_post_body(
    clean: dict, *, sighted_local: str, location_line: str,
    media_urls: list[str], gallery_url: str, attribution: str = "",
    media_provenance: dict | None = None,
) -> str:
    facts = [f"**When:** {sighted_local} ({clean['tz_name']})"]
    if location_line:
        facts.append(f"**Where:** {location_line}")
    if clean.get("num_objects"):
        facts.append(f"**Objects:** {clean['num_objects']}")
    if clean.get("shape"):
        facts.append(f"**Shape:** {clean['shape']}")
    if clean.get("distance"):
        facts.append(f"**Closest distance:** {clean['distance']}")
    if clean.get("apparent_size"):
        facts.append(f"**Apparent size (at arm's length):** {clean['apparent_size']}")
    if clean.get("movement"):
        facts.append("**Movement:** " + ", ".join(clean["movement"]))
    if clean.get("duration_seconds"):
        facts.append(f"**Duration:** {humanize_duration(clean['duration_seconds'])}")
    features = (
        ("wings", clean.get("has_wings")),
        ("rotors", clean.get("has_rotors")),
        ("exhaust plume", clean.get("has_plume")),
        ("noise", clean.get("makes_noise")),
    )
    feature_bits = [f"{label}: {value}" for label, value in features if value]
    if feature_bits:
        facts.append("**Features:** " + " · ".join(feature_bits))
    obs_bits = [f"{q.rstrip('?').lower()}: {clean[key]}"
                for key, q, _h in OBSERVABLES if clean.get(key)]
    if obs_bits:
        facts.append("**Five observables:** " + " · ".join(obs_bits))
    if clean.get("witnesses"):
        facts.append(f"**Witnesses:** {clean['witnesses']}")
    if clean.get("rule_out"):
        facts.append(f"**Why not a common object:** {clean['rule_out']}")
    if clean.get("capture_device"):
        facts.append(f"**Captured on:** {clean['capture_device']}")
    if media_provenance and not media_provenance.get("original"):
        facts.append(f"**⚠️ Media note:** {media_provenance['detail']}")
    if clean.get("sensors"):
        facts.append("**Sensor detection:** " + ", ".join(clean["sensors"]))
    if clean.get("witness_background"):
        facts.append("**Reporter background:** " + ", ".join(clean["witness_background"]))
    parts = ["  \n".join(facts), clean["description"].strip()]
    if media_urls:
        parts.append("**Media:**\n\n" + "\n".join(f"- {u}" for u in media_urls))
    if attribution:
        parts.append(attribution)
    parts.append(
        f"[Original-quality media and full report]({gallery_url}) — Reddit re-encodes "
        f"uploads; the gallery keeps the untouched originals for analysis. "
        f"*Submitted via [ufosighting.report](https://ufosighting.report)*"
    )
    return "\n\n".join(parts)
