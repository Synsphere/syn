from dataclasses import replace
from datetime import timedelta
import math
import pytest
from syn.positions.types import (Estimate, Space, WGS84, canonical, geographic,
                                 horizontal_distance, choose_estimate, Reading)
from syn.positions.config import PositionConfig


def test_roundtrip_and_independent_spaces(now):
    a = geographic(51.5, -0.1, 25, 7, now, "gps", altitudeDatum="ellipsoid")
    assert Estimate.from_dict(a.to_dict()) == a
    room = Space("syn://thing/lab-frame", "syn://type/lab-position", ("x","y"), ("metre","metre"), "metre", "euclidean")
    b = Estimate(room.reference, room.typeRef, {"x": 1, "y": 2}, .05, "metre", now, "robot")
    b.validate_space(room)
    with pytest.raises(ValueError): horizontal_distance(a,b)
    with pytest.raises(ValueError): choose_estimate(a,b,now=now)


@pytest.mark.parametrize("lat,lon,error", [(91,0,1),(-91,0,1),(0,181,1),(0,-181,1),(float("nan"),0,1),(0,0,float("inf")),(0,0,-1),(True,0,1)])
def test_invalid_geo(now,lat,lon,error):
    with pytest.raises(ValueError): geographic(lat,lon,None,error,now,"gps")


def test_unknown_not_zero_and_negative_zero_hash(now):
    a = geographic(0., -0., None, None, now, "gps")
    assert a.error is None
    assert canonical({"a": -0.0}) == canonical({"a": 0})
    with pytest.raises(TypeError): a.coordinates["latitude"] = 4
    with pytest.raises(ValueError): geographic(0,0,None,1,now.replace(tzinfo=None),"gps")


def test_geometry_dateline(now):
    a = geographic(0,179.999,None,1,now,"a")
    b = geographic(0,-179.999,None,1,now,"b")
    assert 221 < horizontal_distance(a,b) < 224


def test_reconciliation(now):
    old = geographic(51,-.1,20,5,now,"gps",altitudeDatum="ellipsoid")
    assert choose_estimate(old,old,now=now) == "unchanged"
    stale = replace(old, observedAt=now-timedelta(minutes=10))
    assert choose_estimate(old,stale,now=now) == "stale"
    future = replace(old,observedAt=now+timedelta(minutes=1))
    assert choose_estimate(old,future,now=now) == "future"
    worse = replace(old,error=500,observedAt=now+timedelta(seconds=1))
    assert choose_estimate(old,worse,now=worse.observedAt) == "less-precise"
    moved = replace(worse,coordinates={"latitude":52,"longitude":-.1,"altitude":20})
    assert choose_estimate(old,moved,now=moved.observedAt) == "replace"
    prior = replace(old,source="timezone",error=100000)
    assert choose_estimate(prior,old,now=now) == "replace"


@pytest.mark.parametrize("options", [{"pollSeconds":0},{"pollSeconds":True},{"rescanSeconds":1},
    {"readSeconds":float("nan")},{"maxDevices":0},{"serialBaudRates":[0]},
    {"nativeServices":"true"},{"unknown":1}])
def test_invalid_config(options):
    with pytest.raises((ValueError,TypeError)): PositionConfig.from_dict(options)


def test_reading_delivery_idempotence(now):
    estimate = geographic(1,2,None,3,now,"device")
    a = Reading("device",now,now,estimate,"2d")
    assert a.sampleKey == replace(a,receivedAt=now+timedelta(seconds=1)).sampleKey
    with pytest.raises(ValueError): replace(a,fix="none")


def test_nested_metadata_and_space_axes_are_immutable(now):
    from syn.positions.types import geographic, Space, finite
    source={"nested":{"items":[1,2]}}
    value=geographic(1,2,None,3,now,"test",provenance=source)
    source["nested"]["items"].append(3)
    assert value.to_dict()["provenance"]=={"nested":{"items":[1,2]}}
    with pytest.raises(TypeError):value.provenance["nested"]["items"][0]=5
    axes=["x"]
    space=Space("syn://thing/immutable-frame","syn://type/immutable-frame-position",axes,["metre"],"metre")
    axes.append("y")
    assert space.axes==("x",)
    with pytest.raises(ValueError):finite(10**10000,"oversized")
