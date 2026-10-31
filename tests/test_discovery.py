import asyncio
import time

from fujiautosave.discovery import IMPORT_REPLY, REGISTER_REPLY, Message, notify_message, read_message

# Verbatim from the 2026-10-31 captures; the byte after HOST's colon is garbage the camera sends
DISCOVER = (b"DISCOVER Stockholm HTTP/1.1\r\nMX:3\r\nHOST:192.168.1.255:\xa6\r\nDSCADDR:192.168.1.228\r\n"
            b"SERVICE:PCAUTOSAVE/1.0\r\n")
REGISTER = (b"REGISTER * HTTP/1.1\r\nHOST:192.168.1.43:51542\r\nDSCNAME:FUJIFILM-X-E2-BD35\r\n"
            b"DSCMODEL:X-E2\r\n")
IMPORT = b"IMPORT /guest HTTP/1.1\r\nHOST:192.168.1.43:51541\r\nDSCNAME:FUJIFILM-X-E2-BD35\r\nDSCPORT: \r\n"


def test_parse_discover():
    m = Message.parse(DISCOVER)
    assert (m.method, m.target) == ("DISCOVER", "Stockholm")
    assert m.headers["DSCADDR"] == "192.168.1.228"
    assert m.headers["SERVICE"] == "PCAUTOSAVE/1.0"


def test_parse_register_and_import():
    r = Message.parse(REGISTER)
    assert (r.method, r.headers["DSCNAME"], r.headers["DSCMODEL"]) == ("REGISTER", "FUJIFILM-X-E2-BD35", "X-E2")
    i = Message.parse(IMPORT)
    assert (i.method, i.target, i.headers["DSCPORT"]) == ("IMPORT", "/guest", "")


def test_our_messages_match_pc_autosave():
    assert notify_message("192.168.1.228", "Stockholm") == (
        b"NOTIFY * HTTP/1.1\r\nHOST: 192.168.1.228:51540\r\nIMPORTER: Stockholm\r\n\r\n")
    assert REGISTER_REPLY == b"HTTP/1.1 200 OK\r\nFOLDER: guest\r\nServiceName: PCAUTOSAVE/1.0\r\n\r\n"
    assert IMPORT_REPLY == b"HTTP/1.1 200 OK\r\n\r\n"
    assert Message.parse(b"HTTP/1.1 200 OK\r\n").ok


async def test_read_message_without_empty_line_does_not_wait_for_idle():
    reader = asyncio.StreamReader()
    reader.feed_data(IMPORT)
    start = time.monotonic()
    m = await read_message(reader, until_header="DSCPORT", idle=5)
    assert m.method == "IMPORT"
    assert time.monotonic() - start < 1


async def test_read_message_falls_back_to_idle():
    reader = asyncio.StreamReader()
    reader.feed_data(REGISTER)
    m = await read_message(reader, idle=0.2)
    assert m.headers["DSCMODEL"] == "X-E2"
