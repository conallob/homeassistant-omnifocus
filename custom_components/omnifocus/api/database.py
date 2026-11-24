"""Replay OmniFocus ``contents.xml`` transactions into an in-memory database."""

from __future__ import annotations

import plistlib
from datetime import datetime
from typing import Any
from xml.etree import ElementTree as ET

from .model import OFFolder, OFPerspective, OFTag, OFTask


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(el: ET.Element) -> dict[str, ET.Element]:
    """Map local tag name -> first child with that name."""
    out: dict[str, ET.Element] = {}
    for child in el:
        out.setdefault(_local(child.tag), child)
    return out


def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def _note_text(el: ET.Element) -> str:
    """Plain text of a ``<note>``: rich ``<text><p>`` markup or a bare string."""
    paragraphs = [c for c in el.iter() if _local(c.tag) == "p"]
    if paragraphs:
        return "\n".join("".join(p.itertext()) for p in paragraphs)
    return "".join(el.itertext())


def _or_none(value: str) -> str | None:
    return value or None


def _int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


def parse_of_datetime(value: str | None, tz: Any = None) -> datetime | None:
    """Parse an OmniFocus timestamp.

    UTC stamps end in ``Z``. Floating wall-clock stamps (due/defer) carry no zone
    and are interpreted in ``tz`` (aware result) when provided.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None and tz is not None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed


def _strip_namespaces(el: ET.Element) -> None:
    for node in el.iter():
        node.tag = _local(node.tag)


def _decode_perspective(el: ET.Element) -> dict[str, Any] | None:
    """Decode the plist embedded in a ``<perspective>`` element, if any."""
    plist_el = next((c for c in el.iter() if _local(c.tag) == "plist"), None)
    if plist_el is None:
        return None
    _strip_namespaces(plist_el)
    try:
        value = plistlib.loads(ET.tostring(plist_el))
    except Exception:
        return None
    return value if isinstance(value, dict) else None


class Database:
    """A reconstructed OmniFocus database. Apply transactions in order, then read."""

    def __init__(self) -> None:
        self.tasks: dict[str, OFTask] = {}
        self.folders: dict[str, OFFolder] = {}
        self.tags: dict[str, OFTag] = {}
        self.perspectives: dict[str, OFPerspective] = {}
        self._tag_joins: dict[str, tuple[str, str]] = {}  # join id -> (task, tag)
        self._children: dict[str, list[str]] | None = None
        self._tag_ids: dict[str, set[str]] | None = None

    # ---- applying transactions -------------------------------------------------

    def apply_transaction(self, contents_xml: str | bytes) -> None:
        """Apply one ``contents.xml`` document."""
        root = ET.fromstring(contents_xml)  # noqa: S314 - MAC-verified, decrypted payload
        for el in root:
            kind = _local(el.tag)
            handler = {
                "task": self._apply_task,
                "folder": self._apply_folder,
                "context": self._apply_tag,
                "task-to-tag": self._apply_task_to_tag,
                "perspective": self._apply_perspective,
            }.get(kind)
            if handler:
                handler(el)
        self._children = None
        self._tag_ids = None

    @staticmethod
    def _op(el: ET.Element) -> str:
        op = el.get("op")
        return op if op in ("update", "delete", "reference") else "create"

    def _apply_task(self, el: ET.Element) -> None:
        task_id = el.get("id")
        op = self._op(el)
        if not task_id or op == "reference":
            return
        if op == "delete":
            self.tasks.pop(task_id, None)
            return
        task = self.tasks.get(task_id) or OFTask(task_id)
        c = _children(el)
        if "name" in c:
            task.name = _text(c["name"])
        for key, attr in (
            ("note", "note"),
            ("added", "added"),
            ("modified", "modified"),
            ("completed", "completed"),
            ("due", "due"),
            ("start", "defer"),
            ("order", "order"),
            ("hidden", "dropped"),
            ("repetition-rule", "repetition_rule"),
        ):
            if key in c:
                value = _note_text(c[key]) if key == "note" else _text(c[key])
                setattr(task, attr, _or_none(value))
        if "flagged" in c:
            task.flagged = _text(c["flagged"]) == "true"
        if "inbox" in c:
            task.in_inbox = _text(c["inbox"]) == "true"
        if "rank" in c:
            task.rank = _int(_text(c["rank"]))
        if "task" in c:
            task.parent_id = c["task"].get("idref") or None
        if "context" in c:
            task.primary_tag_id = c["context"].get("idref") or None
        if "project" in c:
            project = c["project"]
            if len(project):
                pc = _children(project)
                task.is_project = True
                folder = pc.get("folder")
                task.project_folder_id = folder.get("idref") if folder is not None else None
                if "status" in pc:
                    task.project_status = _or_none(_text(pc["status"]))
            else:  # an empty <project/> means "not a project"
                task.is_project = False
                task.project_folder_id = None
                task.project_status = None
        self.tasks[task_id] = task

    def _apply_folder(self, el: ET.Element) -> None:
        folder_id = el.get("id")
        op = self._op(el)
        if not folder_id or op == "reference":
            return
        if op == "delete":
            self.folders.pop(folder_id, None)
            return
        folder = self.folders.get(folder_id) or OFFolder(folder_id)
        c = _children(el)
        if "name" in c:
            folder.name = _text(c["name"])
        if "folder" in c:
            folder.parent_id = c["folder"].get("idref") or None
        self.folders[folder_id] = folder

    def _apply_tag(self, el: ET.Element) -> None:
        tag_id = el.get("id")
        op = self._op(el)
        if not tag_id or op == "reference":
            return
        if op == "delete":
            self.tags.pop(tag_id, None)
            return
        tag = self.tags.get(tag_id) or OFTag(tag_id)
        c = _children(el)
        if "name" in c:
            tag.name = _text(c["name"])
        if "context" in c:
            tag.parent_id = c["context"].get("idref") or None
        self.tags[tag_id] = tag

    def _apply_task_to_tag(self, el: ET.Element) -> None:
        join_id = el.get("id")
        op = self._op(el)
        if not join_id or op == "reference":
            return
        if op == "delete":
            self._tag_joins.pop(join_id, None)
            return
        c = _children(el)
        task_ref = c["task"].get("idref") if "task" in c else None
        tag_ref = c["context"].get("idref") if "context" in c else None
        if task_ref and tag_ref:
            self._tag_joins[join_id] = (task_ref, tag_ref)

    def _apply_perspective(self, el: ET.Element) -> None:
        pid = el.get("id")
        op = self._op(el)
        if not pid or op == "reference":
            return
        if op == "delete":
            self.perspectives.pop(pid, None)
            return
        persp = self.perspectives.get(pid) or OFPerspective(pid)
        rules = _decode_perspective(el)
        if rules is not None:
            persp.rules = rules
            if isinstance(rules.get("name"), str):
                persp.name = rules["name"]
        c = _children(el)
        if "name" in c and _text(c["name"]):
            persp.name = _text(c["name"])
        self.perspectives[pid] = persp

    # ---- queries ---------------------------------------------------------------

    def children_of(self, task_id: str) -> list[OFTask]:
        """Direct child tasks of ``task_id`` ordered by rank."""
        if self._children is None:
            index: dict[str, list[str]] = {}
            for t in self.tasks.values():
                if t.parent_id:
                    index.setdefault(t.parent_id, []).append(t.id)
            self._children = index
        kids = [self.tasks[i] for i in self._children.get(task_id, []) if i in self.tasks]
        return sorted(kids, key=lambda t: (t.rank is None, t.rank or 0))

    def tag_ids_for(self, task: OFTask) -> set[str]:
        """All tag ids on a task (primary tag plus joins)."""
        if self._tag_ids is None:
            index: dict[str, set[str]] = {}
            for task_id, tag_id in self._tag_joins.values():
                index.setdefault(task_id, set()).add(tag_id)
            self._tag_ids = index
        ids = set(self._tag_ids.get(task.id, ()))
        if task.primary_tag_id:
            ids.add(task.primary_tag_id)
        return ids

    def project_of(self, task: OFTask) -> OFTask | None:
        """The project containing ``task`` (or the task itself if a project)."""
        cur: OFTask | None = task
        seen: set[str] = set()
        while cur is not None and cur.id not in seen:
            seen.add(cur.id)
            if cur.is_project:
                return cur
            cur = self.tasks.get(cur.parent_id) if cur.parent_id else None
        return None

    def ancestors(self, task: OFTask) -> list[OFTask]:
        """Ancestors from the immediate parent up to the project."""
        out: list[OFTask] = []
        seen = {task.id}
        cur = self.tasks.get(task.parent_id) if task.parent_id else None
        while cur is not None and cur.id not in seen:
            out.append(cur)
            seen.add(cur.id)
            cur = self.tasks.get(cur.parent_id) if cur.parent_id else None
        return out

    def folder_path(self, folder_id: str | None) -> str | None:
        """``Parent / Child`` path for a folder id."""
        parts: list[str] = []
        seen: set[str] = set()
        cur = folder_id
        while cur and cur not in seen:
            seen.add(cur)
            folder = self.folders.get(cur)
            if folder is None:
                break
            parts.insert(0, folder.name)
            cur = folder.parent_id
        return " / ".join(parts) or None

    def projects(self, *, include_finished: bool = False) -> list[OFTask]:
        """Projects, by default only those still live (not done/dropped)."""
        return [
            t
            for t in self.tasks.values()
            if t.is_project
            and (
                include_finished
                or (
                    t.project_status not in ("done", "dropped")
                    and not t.completed
                    and not t.dropped
                )
            )
        ]
