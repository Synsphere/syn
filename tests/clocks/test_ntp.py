import socket
import struct
import threading
from datetime import UTC, datetime

import pytest
from syn.clocks.ntp import decode_reply, decode_time, encode_time, poll

T1 = 1_790_000_000_000_000_000
M1 = 100_000_000_000


def packet(t1=T1, t2=T1+15_000_000, t3=T1+17_000_000):
    data = bytearray(48)
    data[0], data[1] = 0x24, 2
    data[3:4] = struct.pack("!b", -20)
    data[24:32] = encode_time(t1)
    data[32:40] = encode_time(t2)
    data[40:48] = encode_time(t3)
    return data


def decode(data):
    return decode_reply(data, encode_time(T1), T1, T1+22_000_000, M1, M1+22_000_000, "ntp:test")


def test_four_timestamp_offset_sign_and_delay():
    value = decode(packet())
    assert value.offsetMs == pytest.approx(5, abs=.000002)
    assert value.metadata["roundTripMs"] == pytest.approx(20, abs=.000001)
    assert value.uncertaintyMs >= 10
    assert value.metadata["authenticated"] is False


@pytest.mark.parametrize("index,value", [(0,0x23),(0,0xe4),(1,16),(1,0)])
def test_invalid_server_flags(index,value):
    data=packet();data[index]=value
    with pytest.raises(ValueError):
        decode(data)


def test_origin_and_zero_times_rejected():
    data=packet();data[24:32]=bytes(8)
    with pytest.raises(ValueError,match="origin"):
        decode(data)
    data=packet();data[40:48]=bytes(8)
    with pytest.raises(ValueError,match="zero"):
        decode(data)


@pytest.mark.parametrize("length", [0,47,49,2049])
def test_invalid_lengths(length):
    with pytest.raises(ValueError):
        decode(bytes(length))


def test_clock_step_rejected():
    with pytest.raises(ValueError,match="stepped"):
        decode_reply(packet(),encode_time(T1),T1,T1+10**9,M1,M1+22_000_000,"ntp:test")


@pytest.mark.parametrize("year", [2026,2036,2040,2060])
def test_era_unfolding(year):
    ns=int(datetime(year,3,1,tzinfo=UTC).timestamp())*10**9+123456789
    assert abs(decode_time(encode_time(ns), ns)-ns) <= 1


def test_real_loopback_udp_exchange():
    errors=[]
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as server:
        server.bind(("127.0.0.1",0));server.settimeout(3)
        port=server.getsockname()[1]
        def serve():
            try:
                request,address=server.recvfrom(512)
                import time
                reference=time.time_ns()+50_000_000
                data=packet(time.time_ns(),reference,reference)
                data[24:32]=request[40:48]
                server.sendto(data,address)
            except BaseException as error:
                errors.append(error)
        thread=threading.Thread(target=serve);thread.start()
        result=poll("127.0.0.1",2,port=port)
        thread.join(3)
    assert not errors
    assert result.valid and 30 < result.offsetMs < 100
