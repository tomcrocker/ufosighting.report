"""Sky context for a sighting: external investigation links + a Reddit-markdown
rendering of them and of the computed overhead-satellite data.

The links need only lat/lon/time, so they exist the moment a sighting is posted.
The computed passes (`sky_events`) are produced later by the background worker
— a sighting isn't `live` until after the bot posts, and the worker only picks
up live rows — so the worker edits them into the already-posted comment.
"""


def links(lat, lon, sighted_at: str) -> dict | None:
    """Deterministic external sky/aircraft URLs. None when not geocoded."""
    if lat is None or lon is None or not sighted_at:
        return None
    day, hhmm = sighted_at[:10], sighted_at[11:16]
    return {
        # tar1090 playback: ?replay=YYYY-MM-DD-HH:MM rewinds the whole area to
        # that moment (showTrace needs a specific airframe)
        "adsb": (f"https://globe.adsbexchange.com/?lat={lat:.3f}&lon={lon:.3f}"
                 f"&zoom=9&replay={day}-{hhmm}"),
        # FR24 parses >2 decimals as a flight callsign ("flight not found")
        "fr24": f"https://www.flightradar24.com/{lat:.2f},{lon:.2f}/9",
        "heavens": f"https://www.heavens-above.com/?lat={lat:.4f}&lng={lon:.4f}",
        # in-the-sky honors date params (location is a one-time setting there)
        "skychart": (f"https://in-the-sky.org/skymap.php?year={day[:4]}"
                     f"&month={int(day[5:7])}&day={int(day[8:10])}"
                     f"&latitude={lat:.4f}&longitude={lon:.4f}"),
    }


def _computed_lines(sats: dict) -> list[str]:
    """Bullets for the computed passes, mirroring the detail page's precedence:
    launches and the ISS always call out; a Starlink train supersedes the
    bright-satellite list; an explicit all-clear when nothing was up there."""
    out = []
    for l in sats.get("launches") or []:
        when = "before" if l.get("minutes_after", 0) >= 0 else "after"
        out.append(
            f"* 🚀 **Rocket launch {abs(l['minutes_after'])} min {when} this sighting:** "
            f"{l['provider']} {l['name']} from {l['pad']}, ~{l['distance_km']} km away. "
            f"Twilight launches produce glowing plumes widely reported as UFOs.")
    iss = sats.get("iss")
    if iss:
        out.append(
            f"* 🛰️ **The ISS was overhead:** {iss['alt']}° above the {iss['az']} "
            f"horizon at {iss['time']} UTC, the brightest object in the night sky "
            f"after the Moon.")
    trains = sats.get("trains") or []
    bright = sats.get("bright") or []
    if trains:
        for t in trains:
            out.append(
                f"* 🚨 **Starlink train overhead:** {t['count']} satellites from one "
                f"launch ({t['az']}, around {t['time']} UTC).")
    elif bright:
        listed = "; ".join(
            f"{b['name']} ({b['alt']}° above {b['az']} at {b['time']} UTC)"
            for b in bright)
        out.append(f"* **Satellites overhead at that time:** {listed}")
    else:
        extra = (f" ({sats['starlink_visible']} faint Starlink in view)"
                 if sats.get("starlink_visible") else "")
        out.append(f"* ✅ **No bright satellites were visible overhead**{extra}.")
    return out


def markdown(sky: dict | None, sats: dict | None = None) -> str:
    """The 'Sky context' comment block. Empty string when not geocoded."""
    if not sky:
        return ""
    lines = ["**Sky context for this time and place:**", ""]
    if sats and sats.get("checked"):
        lines += _computed_lines(sats)
        lines.append(f"* *Computed from orbital data ({sats.get('catalog_date')} catalog).*")
    # The first two rewind to the sighting's own date/time; the last two show
    # current conditions. Say so plainly — readers were skimming past the ADS-B
    # link and assuming there was no historical playback.
    lines += [
        f"* [Aircraft overhead at that exact minute]({sky['adsb']}) — ADS-B Exchange "
        f"historical playback (free, no account needed)",
        f"* [Sky chart for that date]({sky['skychart']}) — in-the-sky.org planetarium "
        f"(set your location once on their site)",
        f"* [Satellite passes at this spot]({sky['heavens']}) — Heavens-Above; shows "
        f"upcoming passes, not the sighting date",
        f"* [Live air traffic now]({sky['fr24']}) — FlightRadar24 over this spot",
    ]
    return "\n".join(lines)
