"""End-to-end tests of the sync client against the fake server."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from custom_components.omnifocus.api import (
    OmniFocusAuthError,
    OmniFocusClient,
    OmniFocusError,
    OmniFocusPassphraseError,
    OmniFocusReadOnlyError,
    Target,
)
from tests.fake_server import PASSWORD, USER, FakeOmniServer, contents, task_xml

TZ = timezone(timedelta(hours=2))
INBOX = Target("inbox")
BASE = "20260101000000=aaaaaaaaaaa+bbbbbbbbbbb.zip"
BASELINE = "00000000000000=zzzzzzzzzzz+aaaaaaaaaaa.zip"


async def _executor(func, *args):
    return await asyncio.to_thread(func, *args)


def seed(server: FakeOmniServer) -> None:
    """A small database: inbox, one project with a sequential group, a folder."""
    now = datetime.now(UTC)
    recent = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    old = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    server.add_transaction(
        BASELINE,
        contents(
            '<folder id="fold1"><name>Home</name></folder>',
            task_xml("inbox1", "Buy milk", inbox=True, rank=1, due="2026-07-08T17:00:00.000"),
            task_xml("inbox2", "Call mum", inbox=True, rank=2, note="after 6pm"),
            task_xml("inbox3", "Old done", inbox=True, rank=3, completed=old),
            task_xml("inbox4", "Fresh done", inbox=True, rank=4, completed=recent),
            task_xml("proj1", "Renovate", project="active", folder="fold1"),
            task_xml("p1", "Pick paint", parent="proj1", rank=1),
            task_xml("p2", "Buy brushes", parent="proj1", rank=2),
            task_xml("other", "Unrelated", project="active"),
            task_xml("o1", "Elsewhere", parent="other"),
        ),
    )


@pytest.fixture
async def server() -> AsyncIterator[tuple[FakeOmniServer, TestServer]]:
    fake = FakeOmniServer()
    seed(fake)
    srv = TestServer(fake.app())
    await srv.start_server()
    yield fake, srv
    await srv.close()


def make_client(
    srv: TestServer, session: aiohttp.ClientSession, *, read_only: bool = False, **kw
) -> OmniFocusClient:
    return OmniFocusClient(
        session,
        username=kw.pop("username", USER),
        password=kw.pop("password", PASSWORD),
        passphrase=kw.pop("passphrase", None),
        run_in_executor=_executor,
        client_id="clientAAAAA",
        host_id="HOST-UUID",
        client_name="HA test",
        registration_date=datetime(2026, 1, 1, tzinfo=UTC),
        base_url=str(srv.make_url("/entry")),
        read_only=read_only,
    )


@pytest.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as s:
        yield s


async def test_inbox_and_project_snapshot(server, session) -> None:
    fake, srv = server
    client = make_client(srv, session)
    proj = Target("project", "proj1")
    snap = await client.async_refresh([INBOX, proj], TZ)

    assert fake.shard_hits >= 1  # redirect was followed
    inbox = snap.results[INBOX]
    assert [i.name for i in inbox.items] == ["Buy milk", "Call mum", "Fresh done"]
    assert inbox.items[0].due == datetime(2026, 7, 8, 17, tzinfo=TZ)
    assert inbox.items[1].note == "after 6pm"
    assert inbox.items[2].completed and not inbox.items[0].completed

    assert [i.name for i in snap.results[proj].items] == ["Pick paint", "Buy brushes"]
    assert snap.projects["proj1"] == "Home / Renovate"
    assert "other" in snap.projects


async def test_missing_project_target(server, session) -> None:
    _, srv = server
    snap = await make_client(srv, session).async_refresh([Target("project", "nope")], TZ)
    assert snap.results[Target("project", "nope")].found is False


async def test_incremental_refresh_only_fetches_new(server, session) -> None:
    fake, srv = server
    client = make_client(srv, session)
    await client.async_refresh([INBOX], TZ)
    fake.requests.clear()
    await client.async_refresh([INBOX], TZ)
    assert not [r for r in fake.requests if r[0] == "GET"]  # nothing new to download

    fake.add_transaction(
        "20260102000000=bbbbbbbbbbb+ccccccccccc.zip",
        contents(task_xml("inbox9", "Added elsewhere", inbox=True, rank=9)),
    )
    snap = await client.async_refresh([INBOX], TZ)
    # One download (the redirect hop to the shard is not counted).
    gets = [r for r in fake.requests if r[0] == "GET" and not r[1].startswith("/entry/")]
    assert len(gets) == 1
    assert "Added elsewhere" in [i.name for i in snap.results[INBOX].items]


async def test_compaction_triggers_rebuild(server, session) -> None:
    fake, srv = server
    client = make_client(srv, session)
    await client.async_refresh([INBOX], TZ)
    # Another client compacts: baseline replaced, old transactions gone.
    del fake.files[BASELINE]
    fake.add_transaction(
        "00000000000000=zzzzzzzzzzz+qqqqqqqqqqq.zip",
        contents(task_xml("only", "After compaction", inbox=True)),
    )
    snap = await client.async_refresh([INBOX], TZ)
    assert [i.name for i in snap.results[INBOX].items] == ["After compaction"]


async def test_add_task_inbox_and_project(server, session) -> None:
    fake, srv = server
    client = make_client(srv, session)
    await client.async_refresh([INBOX], TZ)
    before = set(fake.txn_names())

    due = datetime(2026, 8, 1, 9, 30, tzinfo=TZ)
    tid = await client.async_add_task(INBOX, "New thing", note="line1\nline2 <&>", due=due)
    pid = await client.async_add_task(Target("project", "proj1"), "Hang door")

    added = set(fake.txn_names()) - before
    assert len(added) == 2
    # Chained: one descends from the baseline head, the next from that one's tail.
    # (Same-second names sort by random id, so order by parent, not by name.)
    new = sorted(added, key=lambda n: not n.split("=")[1].startswith("aaaaaaaaaaa+"))
    first_tail = new[0].split("=")[1].removesuffix(".zip").split("+")[-1]
    assert new[1].split("=")[1].startswith(first_tail + "+")
    assert "<inbox>true</inbox>" in fake.read_transaction(new[0])
    assert "<due>2026-08-01T09:30:00.000</due>" in fake.read_transaction(new[0])

    snap = await client.async_refresh([INBOX, Target("project", "proj1")], TZ)
    by_id = {i.id: i for i in snap.results[INBOX].items}
    assert by_id[tid].name == "New thing"
    assert by_id[tid].note == "line1\nline2 <&>"
    assert pid in {i.id for i in snap.results[Target("project", "proj1")].items}

    # A single, current .client registration, encrypted like everything else.
    assert len(fake.client_names()) == 1


async def test_complete_uncomplete_and_edit(server, session) -> None:
    _fake, srv = server
    client = make_client(srv, session)
    await client.async_refresh([INBOX], TZ)

    await client.async_update_task("inbox1", completed=True)
    snap = await client.async_refresh([INBOX], TZ)
    assert next(i for i in snap.results[INBOX].items if i.id == "inbox1").completed

    await client.async_update_task(
        "inbox1", completed=False, name="Buy oat milk", due=None, note="x"
    )
    snap = await client.async_refresh([INBOX], TZ)
    item = next(i for i in snap.results[INBOX].items if i.id == "inbox1")
    assert (item.completed, item.name, item.due, item.note) == (False, "Buy oat milk", None, "x")


async def test_repeating_task_cannot_be_completed(server, session) -> None:
    fake, srv = server
    fake.add_transaction(
        "20260102000000=bbbbbbbbbbb+ccccccccccc.zip",
        contents(task_xml("rep", "Water plants", inbox=True, repeat=True)),
    )
    client = make_client(srv, session)
    await client.async_refresh([INBOX], TZ)
    with pytest.raises(OmniFocusError, match="Repeating"):
        await client.async_update_task("rep", completed=True)


async def test_delete(server, session) -> None:
    _, srv = server
    client = make_client(srv, session)
    await client.async_refresh([INBOX], TZ)
    await client.async_delete_tasks(["inbox1", "inbox2"])
    snap = await client.async_refresh([INBOX], TZ)
    assert [i.name for i in snap.results[INBOX].items] == ["Fresh done"]


async def test_read_only_never_writes(server, session) -> None:
    fake, srv = server
    client = make_client(srv, session, read_only=True)
    await client.async_refresh([INBOX], TZ)
    before = dict(fake.files)
    with pytest.raises(OmniFocusReadOnlyError):
        await client.async_add_task(INBOX, "nope")
    with pytest.raises(OmniFocusReadOnlyError):
        await client.async_update_task("inbox1", completed=True)
    with pytest.raises(OmniFocusReadOnlyError):
        await client.async_delete_tasks(["inbox1"])
    assert fake.files == before
    assert not [r for r in fake.requests if r[0] in ("PUT", "DELETE")]


async def test_cannot_add_to_perspective(server, session) -> None:
    _, srv = server
    client = make_client(srv, session)
    with pytest.raises(OmniFocusError, match="perspective"):
        await client.async_add_task(Target("perspective", "x"), "nope")


async def test_bad_password(server, session) -> None:
    _, srv = server
    with pytest.raises(OmniFocusAuthError):
        await make_client(srv, session, password="wrong").async_validate()


async def test_bad_passphrase(server, session) -> None:
    _, srv = server
    with pytest.raises(OmniFocusPassphraseError):
        await make_client(srv, session, passphrase="not-it").async_validate()


async def test_custom_passphrase(session) -> None:
    fake = FakeOmniServer(passphrase="different-phrase")
    seed(fake)
    srv = TestServer(fake.app())
    await srv.start_server()
    try:
        client = make_client(srv, session, passphrase="different-phrase")
        snap = await client.async_refresh([INBOX], TZ)
        assert snap.results[INBOX].items
    finally:
        await srv.close()


async def test_unencrypted_store(session) -> None:
    fake = FakeOmniServer(encrypted=False)
    seed(fake)
    srv = TestServer(fake.app())
    await srv.start_server()
    try:
        snap = await make_client(srv, session).async_refresh([INBOX], TZ)
        assert snap.results[INBOX].items
    finally:
        await srv.close()


async def test_same_second_transactions_replay_in_chain_order(server, session) -> None:
    """An add then an update written in the same second must replay add-first."""
    _, srv = server
    client = make_client(srv, session)
    await client.async_refresh([INBOX], TZ)
    tid = await client.async_add_task(INBOX, "Quick")
    await client.async_update_task(tid, name="Quick (renamed)")
    # A fresh client replays purely from the files on the server.
    fresh = make_client(srv, session)
    snap = await fresh.async_refresh([INBOX], TZ)
    assert "Quick (renamed)" in [i.name for i in snap.results[INBOX].items]
