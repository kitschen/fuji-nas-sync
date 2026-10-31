from __future__ import annotations

import os
import socket
from datetime import datetime
from pathlib import Path

import pytest

from fujiautosave.discovery import Ports
from fujiautosave.fake import FakeFile
from fujiautosave.fuji import Format

FIXTURES = Path(__file__).parent / "fixtures"


def _free_port(taken: set[int]) -> int:
    """A port free for both TCP and UDP on localhost."""
    while True:
        with socket.socket() as tcp, socket.socket(type=socket.SOCK_DGRAM) as udp:
            tcp.bind(("127.0.0.1", 0))
            port = tcp.getsockname()[1]
            if port in taken:
                continue
            try:
                udp.bind(("127.0.0.1", port))
            except OSError:
                continue
            taken.add(port)
            return port


@pytest.fixture
def ports() -> Ports:
    taken: set[int] = set()
    return Ports(*(_free_port(taken) for _ in range(4)))


@pytest.fixture
def files() -> list[FakeFile]:
    return [
        FakeFile("DSCF2090.JPG", os.urandom(1_514_922), datetime(2026, 10, 31, 15, 35, 2)),
        FakeFile("DSCF2091.RAF", os.urandom(300_000), datetime(2026, 10, 31, 15, 35, 4), Format.RAF),
        FakeFile("DSCF2094.MOV", os.urandom(2_500_000), datetime(2026, 10, 31, 15, 38, 10), Format.MOV),
    ]
