import math
import pytest
from syn.positions.bootstrap import longitude_span, half_width_meters, bootstrap, catalogue_entry
from syn.positions.config import PositionConfig


def test_antimeridian_width():
    assert longitude_span([179,-179,178]) == pytest.approx(3)
    assert 166000 < half_width_meters([179,-179,178],0) < 168000
    assert half_width_meters([-4,4],60) < half_width_meters([-4,4],0)*.51


def test_fixed_offset_fallback_not_false_precision(now):
    prior = bootstrap(PositionConfig(timezone="Etc/UTC"),now=now)
    assert prior.error > 20_000_000
    assert prior.coordinates["altitude"] is None
    assert prior.provenance["selection"] == "fallback-not-inferred-location"


def test_offline_capital_and_real_geometry(monkeypatch,now):
    class TF:
        def get_geometry(self,**kwargs):
            return [[[(2,48),(5,48),(5,50),(2,50)]]]
    class GC:
        def get_cities(self):
            return {1:{"name":"Paris","countrycode":"FR","timezone":"Europe/Paris","population":2_000_000,
                       "latitude":48.86,"longitude":2.35},
                    2:{"name":"Lyon","countrycode":"FR","timezone":"Europe/Paris","population":500_000,
                       "latitude":45.76,"longitude":4.83}}
        def get_countries(self):return {"FR":{"capital":"Paris"}}
    entry = catalogue_entry("Europe/Paris", TF(), GC())
    assert entry["city"] == "Paris"
    assert entry["selection"] == "national-capital"
    assert entry["errorMeters"] == pytest.approx(half_width_meters([2,5],48.86))
    monkeypatch.setattr("syn.positions.bootstrap.catalogue_entry",lambda zone:entry)
    prior=bootstrap(PositionConfig(timezone="Europe/Paris"),now=now)
    assert prior.error == entry["errorMeters"]
    assert prior.source == "timezone"
