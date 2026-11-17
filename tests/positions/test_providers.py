from datetime import timedelta
import struct
import pytest
from syn.positions.providers.nmea import NmeaParser, sentence, coordinate
from syn.positions.providers.gpsd import parse_tpv, endpoint
from syn.positions.providers.ble import decode
from syn.positions.providers.base import Device
from syn.positions.types import OS_READING
from .conftest import nmea


def test_nmea_date_and_fix(now):
    p=NmeaParser("test")
    r=p.feed(nmea("GPRMC,080000.00,A,5130.000,N,00006.000,W,0.0,0.0,171126,,,A"),receivedAt=now)
    assert r.estimate.coordinates["latitude"] == pytest.approx(51.5)
    assert r.estimate.coordinates["longitude"] == pytest.approx(-.1)
    assert r.observedAt == now and r.fix == "2d"
    r=p.feed(nmea("GPGGA,080000.00,5130.000,N,00006.000,W,1,08,0.9,20.0,M,45.0,M,,"),receivedAt=now)
    assert r.satellitesLocked == 8
    assert r.estimate.error == 4.5
    assert r.estimate.altitudeDatum == "mean-sea-level"
    assert r.estimate.confidenceLevel is None


@pytest.mark.parametrize("quality",[0,3,6,7,8])
def test_nmea_nofix_and_simulation_rejected(now,quality):
    p=NmeaParser("test")
    r=p.feed(nmea(f"GPGGA,080000.00,5130.000,N,00006.000,W,{quality},08,0.9,20.0,M,45.0,M,,"),receivedAt=now)
    assert r.fix == "none" and r.estimate is None


def test_nmea_corruption_and_boundaries(now):
    with pytest.raises(ValueError):
        sentence("$GPRMC,garbage*00")
    with pytest.raises(ValueError):
        coordinate("5160.000","N",True)
    with pytest.raises(ValueError):
        coordinate("18100.00","E",False)
    p=NmeaParser("test")
    with pytest.raises(ValueError):
        p.feed(nmea("GPGGA,1"),receivedAt=now)
    with pytest.raises(ValueError):
        p.epoch("250000",now)


def test_nmea_midnight(now):
    now=now.replace(hour=0,minute=0,second=1)
    p=NmeaParser("test")
    p.feed(nmea("GPRMC,235959.00,A,5130.000,N,00006.000,W,0.0,0.0,161126,,,A"),receivedAt=now-timedelta(seconds=2))
    value=p.feed(nmea("GPGGA,000001.00,5130.000,N,00006.000,W,1,08,1,10,M,0,M,,"),receivedAt=now)
    assert value.observedAt == now


def test_gst_units_and_no_carry(now):
    p=NmeaParser("test")
    p.feed(nmea("GPGST,080000.00,1,1,1,0,3,4,8"),receivedAt=now)
    r=p.feed(nmea("GPGGA,080000.00,5130.000,N,00006.000,W,1,08,1,20,M,45,M,,"),receivedAt=now)
    assert r.estimate.error == 5 and r.estimate.verticalError == 8
    r=p.feed(nmea("GPRMC,080003.00,A,5130.000,N,00006.000,W,0,0,171126,,,A"),receivedAt=now+timedelta(seconds=3))
    assert r.estimate.error is None and r.estimate.coordinates["altitude"] is None


def test_gpsd_full_and_invalid(now):
    device=Device("gpsd","local|/dev/test","receiver","123",{"path":"/dev/test"})
    value={"mode":3,"lat":51.5,"lon":-.1,"altHAE":20,"eph":3,"epv":5,"time":now.isoformat()}
    r=parse_tpv(value,device,{"satellites":[{"used":True},{"used":False}]},receivedAt=now)
    assert r.satellitesLocked == 1 and r.satellitesVisible == 2
    assert r.estimate.altitudeDatum == "ellipsoid"
    assert r.estimate.error == 3
    assert parse_tpv({"mode":1},device,receivedAt=now).estimate is None
    assert parse_tpv({"mode":3,"lat":51,"lon":0},device,receivedAt=now).estimate is None
    value["lat"]=float("nan")
    with pytest.raises(ValueError):
        parse_tpv(value,device,receivedAt=now)


def test_ble_length_status_and_scaling(now):
    d=Device("ble-ln","device")
    raw=struct.pack("<Hii",4 | (1<<7),515000000,-1000000)
    r=decode(raw,d,receivedAt=now)
    assert r.typeRef == OS_READING
    assert r.estimate.coordinates["latitude"] == 51.5
    assert r.estimate.coordinates["longitude"] == -.1
    assert decode(struct.pack("<Hii",4 | (3<<7),515000000,-1000000),d,receivedAt=now).estimate is None
    with pytest.raises(ValueError):
        decode(raw[:-1],d,receivedAt=now)
    with pytest.raises(ValueError):
        decode(raw+b"x",d,receivedAt=now)


@pytest.mark.parametrize("address",["example.com:2947","user:password@127.0.0.1:2947","127.0.0.1:2947/path","127.0.0.1"])
def test_endpoint_validation(address):
    with pytest.raises(ValueError):
        endpoint(address)


def test_numeric_ipv6():
    assert endpoint("[::1]:2947") == ("::1",2947)
