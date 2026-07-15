import build_anomaly as ba


def test_compute_grid_flags_excess_over_population():
    # two areas with equal population; area A reports 10x more sightings than B,
    # so A must score as the anomaly even though population is identical.
    city_pts = [(34.0, -112.0, 100_000), (40.0, -80.0, 100_000)]
    sights = [(34.0, -112.0)] * 20 + [(40.0, -80.0)] * 2
    anom, presence = ba.compute_grid(sights, city_pts)
    ia, ja = ba._cell(34.0, -112.0)
    ib, jb = ba._cell(40.0, -80.0)
    assert anom[ia, ja] > 1.2                       # clear excess over expected
    assert anom[ia, ja] / anom[ib, jb] > 1.4        # A far more anomalous than B
    assert presence[ia, ja] > presence[ib, jb]


def test_compute_grid_ratio_is_one_when_reports_track_population():
    # equal population AND equal reports -> neither is anomalous (~1.0 each)
    city_pts = [(34.0, -112.0, 100_000), (40.0, -80.0, 100_000)]
    sights = [(34.0, -112.0)] * 10 + [(40.0, -80.0)] * 10
    anom, _ = ba.compute_grid(sights, city_pts)
    ia, ja = ba._cell(34.0, -112.0)
    assert 0.7 < anom[ia, ja] < 1.4


def test_compute_grid_empty_is_safe():
    anom, pres = ba.compute_grid([], [])
    assert anom.shape == (ba.NLAT, ba.NLON)
    assert pres.sum() == 0
