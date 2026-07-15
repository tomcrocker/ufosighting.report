"""Precompute the WORLD per-capita anomaly surface for the map's "Anomaly" layer.

anomaly(cell) = local sighting-per-capita rate / REGIONAL sighting-per-capita rate

The denominator is a coarse-scale (~500 km) smooth of the same field — an
adaptive local baseline. Dividing by it cancels each country's overall reporting
level (r/UFOs is English, so raw per-capita just recovers "English-speaking"),
leaving genuine SUB-regional hotspots that pop in any country, independent of
where people live or how much that country reports.

Output is a Web-Mercator transparent PNG draped on the map with L.imageOverlay.
An image overlay renders the field FAITHFULLY (no leaflet.heat point-summing,
which would re-introduce the population-density effect we're removing). Runs
OFFLINE — the 31MB city file + numpy/PIL never touch the 1GB VM.

Inputs:
  --cities   GeoNames cities1000.txt  (download.geonames.org/export/dump)
  --csv      optional sightings CSV (id,lat,lon,country,...); default = query DB
  --out      output PNG (default static/img/anomaly-world.png)

Bounds (must match map.js ANOMALY_BOUNDS): [[-56,-180],[72,180]].
"""
import argparse
import csv
import math
import numpy as np
from PIL import Image, ImageFilter

RES = 0.25
LAT0, LAT1, LON0, LON1 = -56.0, 72.0, -180.0, 180.0   # populated world, Mercator-safe
NLAT = int((LAT1 - LAT0) / RES)
NLON = int((LON1 - LON0) / RES)
SIGMA_FINE = 1.5        # ~40 km local window
SIGMA_COARSE = 20.0     # ~1300 km country/continental baseline: removes the
                        # country-level (English-language) reporting bias while
                        # KEEPING regional hotspots like the desert Southwest
ALPHA = 2.0             # empirical-Bayes prior (sighting-count units)
LO, HI = 1.05, 1.7      # anomaly range mapped to the colour gradient
UPSCALE = 3

_STOPS = [(0.0, (28, 110, 66)), (0.34, (74, 222, 128)), (0.58, (200, 226, 70)),
          (0.74, (250, 204, 21)), (0.87, (251, 146, 60)), (1.0, (255, 66, 42))]


def _inbox(la, lo):
    return LAT0 <= la < LAT1 and LON0 <= lo < LON1


def _cell(la, lo):
    return int((la - LAT0) / RES), int((lo - LON0) / RES)


def _blur(a, sigma):
    radius = max(4, int(round(3 * sigma)))
    x = np.arange(-radius, radius + 1)
    k = np.exp(-(x ** 2) / (2 * sigma ** 2)); k /= k.sum()
    out = np.zeros_like(a)
    for i, w in enumerate(k):
        out += w * np.roll(a, i - radius, axis=0)
    a2 = out.copy(); out[:] = 0
    for i, w in enumerate(k):
        out += w * np.roll(a2, i - radius, axis=1)
    return out


def compute_grid(sight_pts, city_pts):
    """Return (anomaly, presence). anomaly = local per-capita rate / regional
    per-capita rate (adaptive baseline), empirical-Bayes smoothed. presence =
    local smoothed sighting weight (drives display alpha)."""
    sight = np.zeros((NLAT, NLON)); pop = np.zeros((NLAT, NLON))
    for la, lo in sight_pts:
        if _inbox(la, lo):
            i, j = _cell(la, lo); sight[i, j] += 1
    for la, lo, pp in city_pts:
        if pp > 0 and _inbox(la, lo):
            i, j = _cell(la, lo); pop[i, j] += pp
    if sight.sum() == 0 or pop.sum() == 0:
        return np.ones((NLAT, NLON)), np.zeros((NLAT, NLON))
    sf, pf = _blur(sight, SIGMA_FINE), _blur(pop, SIGMA_FINE)
    sc, pc = _blur(sight, SIGMA_COARSE), _blur(pop, SIGMA_COARSE)
    regional_rate = sc / (pc + 1e-9)            # local baseline sightings-per-person
    expected = pf * regional_rate               # what this cell's population predicts
    anom = (sf + ALPHA) / (expected + ALPHA)
    return anom, sf


def _cmap(t):
    for (t0, c0), (t1, c1) in zip(_STOPS, _STOPS[1:]):
        if t <= t1:
            f = (t - t0) / (t1 - t0 + 1e-9)
            return tuple(int(c0[k] + (c1[k] - c0[k]) * f) for k in range(3))
    return _STOPS[-1][1]


def _mercy(lat):
    return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def render_png(anom, presence, out_path):
    inten = np.clip((anom - LO) / (HI - LO), 0, 1) ** 0.85
    # sensitivity: /2 (was /4) lets mid-data countries (CA/AU/DE/BR) render, and
    # weighting alpha toward inten (0.15 + 0.85) keeps hotspots vivid while the
    # empirical-Bayes prior holds 1-2-report noise near baseline (low inten -> faint)
    present = np.clip(presence / 2.0, 0, 1)
    rgba = np.zeros((NLAT, NLON, 4), np.uint8)
    for i in range(NLAT):
        for j in range(NLON):
            if present[i, j] <= 0.02:
                continue
            r, g, b = _cmap(inten[i, j])
            a = present[i, j] * (0.15 + 0.85 * inten[i, j])
            rgba[i, j] = (r, g, b, int(min(a, 1.0) * 255))
    eq = rgba[::-1]  # row 0 -> north (LAT1)
    eq_img = Image.fromarray(eq, "RGBA").resize(
        (NLON * UPSCALE, NLAT * UPSCALE), Image.BILINEAR)
    eq_arr = np.array(eq_img)
    Heq = eq_arr.shape[0]
    # reproject rows: linear-in-latitude -> linear-in-Mercator-Y so it aligns
    # with Leaflet's Web-Mercator basemap under L.imageOverlay
    mtop, mbot = _mercy(LAT1), _mercy(LAT0)
    src = []
    for r in range(Heq):
        my = mtop - (r / (Heq - 1)) * (mtop - mbot)
        lat = math.degrees(2 * math.atan(math.exp(my)) - math.pi / 2)
        sr = (LAT1 - lat) / (LAT1 - LAT0) * (Heq - 1)
        src.append(min(max(int(round(sr)), 0), Heq - 1))
    merc = eq_arr[np.array(src)]
    Image.fromarray(merc, "RGBA").filter(
        ImageFilter.GaussianBlur(UPSCALE * 0.9)).save(out_path)


def load_sightings_csv(path):
    out = []
    with open(path) as f:
        for r in csv.DictReader(f):
            try:
                out.append((float(r["lat"]), float(r["lon"])))
            except (ValueError, KeyError):
                continue
    return out


def load_sightings_db():
    from app import db
    from app.config import get_settings
    c = db.connect(get_settings().db_path)
    rows = c.execute(
        """SELECT lat, lon FROM sightings
           WHERE lat IS NOT NULL AND lon IS NOT NULL
             AND status IN ('live','deleted_by_user','removed_on_reddit')""").fetchall()
    return [(r["lat"], r["lon"]) for r in rows]


def load_cities(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            p = line.split("\t")
            try:
                out.append((float(p[4]), float(p[5]), int(p[14] or 0)))
            except (ValueError, IndexError):
                continue
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cities", required=True)
    ap.add_argument("--csv")
    ap.add_argument("--out", default="static/img/anomaly-world.png")
    a = ap.parse_args()
    sightings = load_sightings_csv(a.csv) if a.csv else load_sightings_db()
    anom, presence = compute_grid(sightings, load_cities(a.cities))
    render_png(anom, presence, a.out)
    print(f"build_anomaly: {len(sightings)} sightings -> {a.out}")


if __name__ == "__main__":
    main()
