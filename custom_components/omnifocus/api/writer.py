"""Build OmniFocus ``contents.xml`` transactions and ``.client`` registrations."""

from __future__ import annotations

import plistlib
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from xml.sax.saxutils import escape

from .crypto import create_id

NAMESPACE = "http://www.omnigroup.com/namespace/OmniFocus/v2"
APP_ID = "com.omnigroup.OmniFocus4"
APP_VERSION = "185.9.1"

_UNSET = object()


def utc_iso(when: datetime | None = None) -> str:
    """UTC timestamp with milliseconds and ``Z`` (added/modified/completed)."""
    when = (when or datetime.now(UTC)).astimezone(UTC)
    return when.strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}Z"


def floating_local(when: datetime) -> str:
    """Wall-clock time without a zone (due/defer). Aware values are used as given."""
    return when.strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}"


def _esc(value: str) -> str:
    return escape(value, {'"': "&quot;"})


def note_xml(text: str) -> str:
    """Render plain text as OmniFocus rich-text note markup."""
    paragraphs = "".join(f"<p><run><lit>{_esc(line)}</lit></run></p>" for line in text.split("\n"))
    return f"<note><text>{paragraphs}</text></note>"


def _wrap(elements: list[str]) -> str:
    body = "\n".join("  " + e for e in elements)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<omnifocus xmlns="{NAMESPACE}" app-id="{APP_ID}" app-version="{APP_VERSION}">\n'
        f"{body}\n</omnifocus>\n"
    )


def _new_rank() -> int:
    """A rank near the top of the signed 32-bit range, where new items cluster."""
    return random.randint(2147480000, 2147483640)  # noqa: S311 - not security relevant


def build_add_task(
    name: str,
    *,
    note: str | None = None,
    due: datetime | None = None,
    flagged: bool = False,
    parent_id: str | None = None,
) -> tuple[str, str]:
    """Build a create transaction. No ``parent_id`` means the inbox."""
    task_id = create_id()
    lines = [f'<task id="{task_id}">', "  <project/>"]
    if parent_id:
        lines.append(f'  <task idref="{parent_id}"/>')
    else:
        lines += ["  <inbox>true</inbox>", "  <task/>"]
    lines.append(f"  <added>{utc_iso()}</added>")
    lines.append(f"  <name>{_esc(name)}</name>")
    if note:
        lines.append("  " + note_xml(note))
    if flagged:
        lines.append("  <flagged>true</flagged>")
    if due is not None:
        lines.append(f"  <due>{floating_local(due)}</due>")
    lines.append(f"  <rank>{_new_rank()}</rank>")
    lines.append("  <order>sequential</order>")
    lines.append("</task>")
    return _wrap(["\n  ".join(lines)]), task_id


def build_update_task(
    task_id: str,
    *,
    added: str | None = None,
    name: str | None = None,
    note: object = _UNSET,
    due: object = _UNSET,
    completed: object = _UNSET,
) -> str:
    """Build an ``op="update"`` transaction. Only the supplied fields change.

    ``note``/``due``/``completed`` accept ``None`` to clear. ``added`` echoes the
    object's existing added date so a partial update does not clobber it.
    """
    lines = [f'<task id="{task_id}" op="update">']
    if added:
        lines.append(f"  <added>{_esc(added)}</added>")
    lines.append(f"  <modified>{utc_iso()}</modified>")
    if name is not None:
        lines.append(f"  <name>{_esc(name)}</name>")
    if note is not _UNSET:
        lines.append("  " + note_xml(str(note)) if note else "  <note/>")
    if due is not _UNSET:
        lines.append(
            f"  <due>{floating_local(due)}</due>" if isinstance(due, datetime) else "  <due/>"
        )
    if completed is not _UNSET:
        lines.append(
            f"  <completed>{utc_iso(completed)}</completed>"
            if isinstance(completed, datetime)
            else "  <completed/>"
        )
    lines.append("</task>")
    return _wrap(["\n  ".join(lines)])


def build_delete_task(task_id: str) -> str:
    """Build an ``op="delete"`` transaction."""
    return _wrap([f'<task id="{task_id}" op="delete"/>'])


@dataclass(slots=True)
class ClientInfo:
    """Identity of this sync client, as registered in its ``.client`` file."""

    client_id: str
    host_id: str
    name: str
    registration_date: datetime
    last_sync_date: datetime
    tail_identifiers: list[str]


def build_client_plist(info: ClientInfo) -> bytes:
    """Build a ``.client`` registration plist."""
    return plistlib.dumps(
        {
            "ApplicationMarketingVersion": "4.8.8",
            "CurrentFrameworkVersion": "2",
            "OFMSyncClientModelVersion": "6.0.18",
            "OFMSyncClientSupportedCapabilities": ["delta_transactions"],
            "XMLSyncClientSupportedCapabilities": [
                "stable_repeats",
                "external_attachments",
                "floating_time_zones",
                "unknown_element_import",
                "versioned_perspectives",
                "delta_transactions",
                "active_object_hidden_dates",
            ],
            "bundleIdentifier": APP_ID,
            "bundleVersion": APP_VERSION,
            "clientIdentifier": info.client_id,
            "hostID": info.host_id,
            "lastSyncDate": info.last_sync_date.replace(tzinfo=None),
            "name": info.name,
            "registrationDate": info.registration_date.replace(tzinfo=None),
            "tailIdentifiers": info.tail_identifiers,
        }
    )
