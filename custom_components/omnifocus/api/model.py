"""Data model for the subset of OmniFocus objects this integration uses."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class OFTask:
    """A task, action group or project (a project is a task with ``is_project``)."""

    id: str
    name: str = ""
    note: str | None = None
    added: str | None = None
    modified: str | None = None
    completed: str | None = None
    due: str | None = None
    defer: str | None = None  # the <start> element
    flagged: bool = False
    parent_id: str | None = None
    is_project: bool = False
    project_folder_id: str | None = None
    project_status: str | None = None  # active | inactive | done | dropped
    in_inbox: bool = False
    rank: int | None = None
    order: str | None = None  # parallel | sequential
    dropped: str | None = None  # the <hidden> element
    repetition_rule: str | None = None
    primary_tag_id: str | None = None


@dataclass(slots=True)
class OFFolder:
    """A folder of projects."""

    id: str
    name: str = ""
    parent_id: str | None = None


@dataclass(slots=True)
class OFTag:
    """A tag (a ``<context>`` element in the XML)."""

    id: str
    name: str = ""
    parent_id: str | None = None


@dataclass(slots=True)
class OFPerspective:
    """A custom perspective. ``rules`` is the decoded archived plist, if readable."""

    id: str
    name: str = ""
    rules: dict[str, Any] | None = field(default=None)
