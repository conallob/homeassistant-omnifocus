"""The check_login script works end to end against the fake server."""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest
from aiohttp.test_utils import TestServer

from tests.api.test_client import seed
from tests.fake_server import PASSWORD, USER, FakeOmniServer

SCRIPT = Path(__file__).parent.parent.parent / "scripts" / "check_login.py"


@pytest.fixture
async def srv():
    fake = FakeOmniServer()
    seed(fake)
    server = TestServer(fake.app())
    await server.start_server()
    yield server
    await server.close()


async def _run(srv, monkeypatch, password: str, passphrase: str = "") -> int:
    monkeypatch.setenv("OF_PASSWORD", password)
    monkeypatch.setenv("OF_PASSPHRASE", passphrase)
    mod = runpy.run_path(str(SCRIPT))
    return await mod["run"](USER, password, passphrase, str(srv.make_url("/entry")))


async def test_ok(srv, monkeypatch, capsys) -> None:
    assert await _run(srv, monkeypatch, PASSWORD) == 0
    out = capsys.readouterr().out
    assert "login accepted" in out and "database read" in out
    assert PASSWORD not in out


async def test_wrong_password_and_whitespace_note(srv, monkeypatch, capsys) -> None:
    assert await _run(srv, monkeypatch, PASSWORD + " ") == 1
    out = capsys.readouterr().out
    assert "leading/trailing whitespace" in out and "login rejected" in out
    assert PASSWORD not in out


async def test_wrong_passphrase(srv, monkeypatch, capsys) -> None:
    assert await _run(srv, monkeypatch, PASSWORD, "nope") == 2
    out = capsys.readouterr().out
    assert "passphrase is wrong" in out
    assert "'prf': 'sha1'" in out and "'rounds': 10" in out
