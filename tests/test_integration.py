"""Integration tests: credentials -> config flow -> subentries -> todo entities."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from aiohttp.test_utils import TestServer
from homeassistant.components.application_credentials import (
    ClientCredential,
    async_import_client_credential,
)
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component

from custom_components.omnifocus.const import DOMAIN
from tests.fake_server import PASSWORD, USER, FakeOmniServer, contents, task_xml

BASELINE = "00000000000000=zzzzzzzzzzz+aaaaaaaaaaa.zip"


@pytest.fixture(autouse=True)
def _sockets(socket_enabled):
    """Loopback sockets for the fake server."""


@pytest.fixture
async def fake() -> AsyncIterator[tuple[FakeOmniServer, TestServer]]:
    server = FakeOmniServer()
    recent = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    server.add_transaction(
        BASELINE,
        contents(
            task_xml("inbox1", "Buy milk", inbox=True, rank=1),
            task_xml("inbox2", "Done already", inbox=True, rank=2, completed=recent),
            task_xml("proj1", "Renovate", project="active"),
            task_xml("p1", "Pick paint", parent="proj1", rank=1),
        ),
    )
    srv = TestServer(server.app())
    await srv.start_server()
    yield server, srv
    await srv.close()


async def _add_credential(hass: HomeAssistant, password: str = PASSWORD) -> None:
    assert await async_setup_component(hass, "application_credentials", {})
    await async_import_client_credential(
        hass, DOMAIN, ClientCredential(USER, password, name="alice")
    )


async def _create_entry(hass: HomeAssistant, srv: TestServer):
    await _add_credential(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"server_url": str(srv.make_url("/entry"))}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    await hass.async_block_till_done()
    return result["result"]


async def _add_list(hass, entry, name, target, mode, ref_field=None, ref=None):
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "todo_list"), context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM, result
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": name, "target_type": target, "mode": mode}
    )
    if ref_field:
        assert result["step_id"] == ref_field
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {ref_field: ref}
        )
    await hass.async_block_till_done()
    return result


async def test_missing_credentials_aborts(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "application_credentials", {})
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "missing_credentials"


async def test_bad_password_shows_error(hass: HomeAssistant, fake) -> None:
    _, srv = fake
    await _add_credential(hass, password="wrong")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"server_url": str(srv.make_url("/entry"))}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_bad_password_logs_account_but_not_secret(
    hass: HomeAssistant, fake, caplog: pytest.LogCaptureFixture
) -> None:
    _, srv = fake
    await _add_credential(hass, password="hunter2-wrong")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert USER in result["description_placeholders"]["accounts"]
    await hass.config_entries.flow.async_configure(
        result["flow_id"], {"server_url": str(srv.make_url("/entry"))}
    )
    assert f"account '{USER}'" in caplog.text
    assert "hunter2-wrong" not in caplog.text
    # Describes the stored secret by length/shape only, so it can be compared.
    assert f"client secret is {len('hunter2-wrong')} characters;" in caplog.text


async def test_secret_shape_flags_non_ascii(hass: HomeAssistant, fake, caplog) -> None:
    """A non-ASCII secret is called out by shape only, never echoed."""
    _, srv = fake
    await _add_credential(hass, password="pässwörd-wrong")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    await hass.config_entries.flow.async_configure(
        result["flow_id"], {"server_url": str(srv.make_url("/entry"))}
    )
    assert "14 characters, with non-ASCII characters" in caplog.text
    assert "pässwörd" not in caplog.text


async def test_bad_passphrase_shows_error(
    hass: HomeAssistant, fake, caplog: pytest.LogCaptureFixture
) -> None:
    _, srv = fake
    await _add_credential(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"server_url": str(srv.make_url("/entry")), "passphrase": "nope"}
    )
    assert result["errors"] == {"base": "invalid_passphrase"}
    # The log carries the database's non-secret key settings (never the passphrase).
    assert "'prf': 'sha1'" in caplog.text
    assert "nope" not in caplog.text


async def test_password_not_stored_in_entry(hass: HomeAssistant, fake) -> None:
    _, srv = fake
    entry = await _create_entry(hass, srv)
    assert PASSWORD not in str(entry.data)
    assert entry.data["auth_implementation"]
    assert entry.state is ConfigEntryState.LOADED


async def test_inbox_and_project_lists(hass: HomeAssistant, fake) -> None:
    _, srv = fake
    entry = await _create_entry(hass, srv)
    r = await _add_list(hass, entry, "Inbox", "inbox", "read_only")
    assert r["type"] is FlowResultType.CREATE_ENTRY
    r = await _add_list(hass, entry, "Reno", "project", "read_only", "project", "proj1")
    assert r["type"] is FlowResultType.CREATE_ENTRY

    inbox = hass.states.get("todo.inbox")
    reno = hass.states.get("todo.reno")
    assert inbox is not None and reno is not None
    assert inbox.state == "1"  # one needs-action, one completed
    assert reno.state == "1"
    assert reno.attributes["mode"] == "read_only"


async def test_one_to_one_mapping_enforced(hass: HomeAssistant, fake) -> None:
    _, srv = fake
    entry = await _create_entry(hass, srv)
    await _add_list(hass, entry, "Inbox", "inbox", "read_only")
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "todo_list"), context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Inbox 2", "target_type": "inbox", "mode": "read_only"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "already_mapped"}


async def test_read_only_list_rejects_writes_and_never_touches_server(
    hass: HomeAssistant, fake
) -> None:
    server, srv = fake
    entry = await _create_entry(hass, srv)
    await _add_list(hass, entry, "Inbox", "inbox", "read_only")
    before = dict(server.files)
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "todo", "add_item", {"item": "x"}, target={"entity_id": "todo.inbox"}, blocking=True
        )
    assert server.files == before
    assert not [r for r in server.requests if r[0] in ("PUT", "DELETE")]


async def test_read_write_list_round_trip(hass: HomeAssistant, fake) -> None:
    server, srv = fake
    entry = await _create_entry(hass, srv)
    await _add_list(hass, entry, "Inbox", "inbox", "read_write")
    target = {"entity_id": "todo.inbox"}

    await hass.services.async_call(
        "todo",
        "add_item",
        {"item": "Water plants", "description": "front room", "due_date": "2030-01-02"},
        target=target,
        blocking=True,
    )
    result = await hass.services.async_call(
        "todo", "get_items", {}, target=target, blocking=True, return_response=True
    )
    items = {i["summary"]: i for i in result["todo.inbox"]["items"]}
    assert items["Water plants"]["description"] == "front room"
    assert "2030-01-02" in items["Water plants"]["due"]

    await hass.services.async_call(
        "todo",
        "update_item",
        {"item": "Buy milk", "status": "completed"},
        target=target,
        blocking=True,
    )
    result = await hass.services.async_call(
        "todo",
        "get_items",
        {"status": "completed"},
        target=target,
        blocking=True,
        return_response=True,
    )
    assert "Buy milk" in {i["summary"] for i in result["todo.inbox"]["items"]}

    await hass.services.async_call(
        "todo", "remove_item", {"item": "Water plants"}, target=target, blocking=True
    )
    result = await hass.services.async_call(
        "todo", "get_items", {}, target=target, blocking=True, return_response=True
    )
    assert "Water plants" not in {i["summary"] for i in result["todo.inbox"]["items"]}

    # What we wrote is valid, encrypted, and readable by an independent decrypt.
    # File names have one-second resolution, so same-second writes are not in name
    # order; check the set, and that the three form a single parent chain.
    new = [n for n in server.txn_names() if n != BASELINE]
    assert len(new) == 3
    assert any("Water plants" in server.read_transaction(n) for n in new)
    tails = {n.split("=")[1].removesuffix(".zip").split("+")[-1] for n in new}
    parents = {n.split("=")[1].removesuffix(".zip").split("+")[0] for n in new}
    assert len(parents - tails) == 1  # exactly one transaction descends from the baseline


async def test_deleted_project_makes_list_unavailable(hass: HomeAssistant, fake) -> None:
    server, srv = fake
    entry = await _create_entry(hass, srv)
    await _add_list(hass, entry, "Reno", "project", "read_only", "project", "proj1")
    assert hass.states.get("todo.reno").state == "1"
    server.add_transaction(
        "20990101000000=aaaaaaaaaaa+ccccccccccc.zip",
        contents('<task id="proj1" op="delete"/>'),
    )
    coordinator = entry.runtime_data
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("todo.reno").state == "unavailable"


async def test_auth_failure_triggers_reauth(hass: HomeAssistant, fake) -> None:
    server, srv = fake
    entry = await _create_entry(hass, srv)
    server.fail_auth = True
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED  # reauth flow, entry stays
    assert any(
        f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress()
    )
