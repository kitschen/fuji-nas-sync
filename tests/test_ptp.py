import asyncio
import struct

import pytest
from conftest import FIXTURES

from fujiautosave.ptp import (
    END_OF_SESSION,
    Container,
    ContainerType,
    Op,
    PtpConnection,
    ProtocolError,
    SessionEnded,
    decode_init_ack,
    encode_init_ack,
    encode_init_request,
    pack_string,
    read_packet,
    unpack_string,
)

CLIENT = (FIXTURES / "nonew_client.bin").read_bytes()
CAMERA = (FIXTURES / "nonew_camera.bin").read_bytes()


def split_packets(buf: bytes) -> list[bytes]:
    packets, i = [], 0
    while i < len(buf):
        (length,) = struct.unpack_from("<I", buf, i)
        packets.append(buf[i:i + length])
        i += length
    return packets


def test_container_round_trip():
    c = Container(ContainerType.COMMAND, 0x101B, 7, struct.pack("<III", 2, 0x100000, 0x100000))
    assert Container.decode(c.encode()) == c


def test_container_length_mismatch():
    with pytest.raises(ProtocolError):
        Container.decode(bytes.fromhex("10000000010002100100000001000000") + b"x")


def test_captured_client_containers_re_encode_identically():
    for packet in split_packets(CLIENT)[1:]:  # skip the init request
        assert Container.decode(packet).encode() == packet


def test_pack_string_matches_capture():
    assert pack_string("0/2") == bytes.fromhex("04 30 00 2f 00 32 00 00 00")
    assert pack_string("") == b"\x00"
    assert unpack_string(pack_string("3/4:ERR(-1)") + b"tail") == ("3/4:ERR(-1)", 25)


def test_init_request_matches_capture():
    guid = CLIENT[12:28]
    request = encode_init_request(guid, "FUJIFILM-X-E2-BD35")
    assert len(request) == 0x52
    # after the name's terminator PC AutoSave sends uninitialised bytes; we send zeros
    assert request[:66] == CLIENT[:66]


def test_init_ack_from_capture():
    ack = decode_init_ack(CAMERA[:0x44])
    assert ack.model == "X-E2"
    assert ack.guid == bytes.fromhex("0870b0610a8b4593b2e79357dd36e050")
    assert encode_init_ack(ack.guid, ack.model) == CAMERA[:0x44]


def test_init_refused():
    with pytest.raises(ProtocolError):
        decode_init_ack(struct.pack("<III", 0x44, 5, 0x2019).ljust(0x44, b"\0"))


async def test_end_marker_raises_session_ended():
    reader = asyncio.StreamReader()
    reader.feed_data(bytes.fromhex("0c000000030001201c000000") + END_OF_SESSION)
    assert Container.decode(await read_packet(reader, 1)).code == 0x2001
    with pytest.raises(SessionEnded):
        await read_packet(reader, 1)


class RecordingWriter:
    def __init__(self):
        self.calls = []

    def write(self, data):
        self.calls.append(data)

    async def drain(self):
        self.calls.append("drain")


async def test_data_phase_goes_out_after_command_is_flushed():
    """The camera loses a data container sent in the same TCP segment as its command."""
    reader, writer = asyncio.StreamReader(), RecordingWriter()
    reader.feed_data(bytes.fromhex("0c0000000300012001000000"))
    await PtpConnection(reader, writer).transaction(Op.SET_DEVICE_PROP_VALUE, (0xDF01,), bytes([3, 0]))
    assert writer.calls == [bytes.fromhex("100000000100161001000000" "01df0000"), "drain",
                            bytes.fromhex("0e0000000200161001000000" "0300"), "drain"]


async def test_cancelled_transaction_still_consumes_its_response():
    """Otherwise the late response would be taken for the answer to the next command."""
    reader, writer = asyncio.StreamReader(), RecordingWriter()
    conn = PtpConnection(reader, writer)
    first = asyncio.ensure_future(conn.transaction(Op.GET_DEVICE_PROP_VALUE, (0xD212,)))
    await asyncio.sleep(0)
    first.cancel()
    reader.feed_data(bytes.fromhex("0c0000000300012001000000"))  # response to transaction 1
    reader.feed_data(bytes.fromhex("0c0000000300012002000000"))  # response to transaction 2
    response = await asyncio.wait_for(conn.transaction(Op.CLOSE_SESSION), 1)
    assert response.code == 0x2001 and first.cancelled()
