import json
import socket
import sys
import threading
import types
from dataclasses import replace

from syn.positions.config import PositionConfig
from syn.positions.providers.base import Device
from syn.positions.providers import gpsd, network
from .conftest import nmea


def serve(payload):
    ready=threading.Event();details={}
    def worker():
        with socket.socket() as listener:
            listener.bind(("127.0.0.1",0));listener.listen(1);listener.settimeout(3)
            details["port"]=listener.getsockname()[1];ready.set()
            try:
                connection,_=listener.accept()
            except TimeoutError:return
            with connection:
                connection.sendall(payload)
    thread=threading.Thread(target=worker,daemon=True);thread.start();assert ready.wait(2)
    return details["port"],thread


def test_tcp_gpsd_stream(now):
    payload=(json.dumps({"class":"TPV","device":"/dev/test","mode":2,"lat":51,"lon":-.1,"eph":5,"time":now.isoformat()})+"\n").encode()
    port,thread=serve(payload)
    device=Device("gpsd","test",options={"endpoint":f"127.0.0.1:{port}","path":"/dev/test"})
    records=gpsd.poll(device,PositionConfig(readSeconds=.2))
    thread.join(2)
    assert len(records)==1 and records[0].estimate.error==5


def test_gpsd_device_discovery():
    payload=(json.dumps({"class":"DEVICES","devices":[{"path":"/dev/ttyUSB0","driver":"u-blox"}]})+"\n").encode()
    port,thread=serve(payload)
    result=gpsd.probe(f"127.0.0.1:{port}",PositionConfig(probeSeconds=.2));thread.join(2)
    assert result.devices[0].claimedPorts==("/dev/ttyUSB0",)


def test_explicit_tcp_nmea():
    payload=nmea("GPRMC,080000.00,A,5130.000,N,00006.000,W,0,0,171126,,,A").encode()
    port,thread=serve(payload)
    records=network.poll(network.descriptor(f"tcp://127.0.0.1:{port}"),PositionConfig(readSeconds=.2));thread.join(2)
    assert len(records)==1 and records[0].estimate.coordinates["latitude"]==51.5


def test_passive_serial_closes_and_preserves_metadata(monkeypatch):
    from syn.positions.providers import serial as provider
    instances=[]
    class Port:
        def __init__(self,**kwargs):self.options=kwargs;self.count=0;self.closed=False;instances.append(self)
        def open(self):assert self.dtr is False and self.rts is False
        def read_until(self,*args):
            self.count+=1
            if self.count==1:return nmea("GPRMC,080000.00,A,5130.000,N,00006.000,W,0,0,171126,,,A").encode()
            return b""
        def close(self):self.closed=True
        def write(self,*args):raise AssertionError("Probe wrote to unknown hardware")
    fake=types.ModuleType("serial");fake.Serial=Port
    monkeypatch.setitem(sys.modules,"serial",fake)
    device=Device("serial","/dev/fake","Test receiver","123",{"confirmed":False})
    records,options=provider.poll(device,PositionConfig(probeSeconds=.005,readSeconds=.005,serialBaudRates=(9600,4800)))
    assert len(instances)==1 and instances[0].closed
    assert records[0].model=="Test receiver" and records[0].serialNumber=="123"
    assert options["confirmed"] is True and options["baudrate"]==9600


def test_discovery_isolates_provider_failure(monkeypatch):
    from syn.positions import providers
    from syn.positions.providers import serial,native,ble
    from syn.positions.providers.base import Probe
    monkeypatch.setattr(gpsd,"probe",lambda *_:(_ for _ in ()).throw(PermissionError()))
    monkeypatch.setattr(native,"probe",lambda *_:(Probe("modemmanager","no-devices"),()))
    monkeypatch.setattr(ble,"probe",lambda *_:Probe("ble-ln","no-devices"))
    monkeypatch.setattr(serial,"probe",lambda *_:Probe("serial","candidates",[Device("serial","/dev/fake")]))
    found,reports=providers.discover(PositionConfig())
    assert len(found)==1 and found[0].provider=="serial"
    assert sum(r["status"]=="unavailable" for r in reports)==2
