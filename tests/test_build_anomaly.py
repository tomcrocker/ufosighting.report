import build_anomaly as ba


def _patch(center_reports, other_reports, pop=50_000, step=0.75):
    """A grid of equally-populated towns; the centre town reports
    center_reports, every other town reports other_reports."""
    cities, sights = [], []
    clat, clon = 40.0, -100.0
    for di in range(-3, 4):
        for dj in range(-3, 4):
            la, lo = clat + di * step, clon + dj * step
            cities.append((la, lo, pop))
            n = center_reports if (di == 0 and dj == 0) else other_reports
            sights += [(la, lo)] * n
    return cities, sights, (clat, clon)


def test_flags_local_excess_vs_region():
    # centre reports 10x its neighbours at equal population -> anomalous vs region
    cities, sights, (clat, clon) = _patch(center_reports=30, other_reports=3)
    anom, pres = ba.compute_grid(sights, cities)
    ic, jc = ba._cell(clat, clon)
    ie, je = ba._cell(clat + 2 * 0.75, clon + 2 * 0.75)   # an ordinary town
    assert anom[ic, jc] > anom[ie, je]
    assert anom[ic, jc] > 1.3            # clearly above the regional baseline
    assert anom[ie, je] < 1.2            # ordinary town ~ baseline
    assert pres[ic, jc] > pres[ie, je]


def test_uniform_reporting_is_not_anomalous():
    # every town reports the same at equal population -> nobody is a hotspot
    cities, sights, (clat, clon) = _patch(center_reports=5, other_reports=5)
    anom, _ = ba.compute_grid(sights, cities)
    ic, jc = ba._cell(clat, clon)
    assert 0.7 < anom[ic, jc] < 1.3


def test_compute_grid_empty_is_safe():
    anom, pres = ba.compute_grid([], [])
    assert anom.shape == (ba.NLAT, ba.NLON)
    assert pres.sum() == 0
