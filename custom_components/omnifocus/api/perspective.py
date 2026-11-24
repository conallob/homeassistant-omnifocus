"""Best-effort evaluation of OmniFocus availability and custom perspectives.

OmniFocus archives a perspective's filter rules as a plist. The rule vocabulary
is not formally documented, so this implements the commonly seen rules and
reports everything it did not understand via :class:`PerspectiveResult`.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .database import Database, parse_of_datetime
from .model import OFPerspective, OFTask


def is_active(task: OFTask) -> bool:
    """Neither completed nor dropped (ignoring parents)."""
    return (
        not task.completed and not task.dropped and task.project_status not in ("done", "dropped")
    )


def is_leaf(db: Database, task: OFTask) -> bool:
    """True for actions that do not themselves contain further tasks."""
    return not task.is_project and not db.children_of(task.id)


def _container_blocks(db: Database, task: OFTask, now: datetime, tz: Any) -> bool:
    """Whether an ancestor, defer date or sequential order hides ``task``."""
    chain = [task, *db.ancestors(task)]
    for node in chain:
        if node.is_project and node.project_status in ("inactive", "done", "dropped"):
            return True
        if node is not task and (node.completed or node.dropped):
            return True
        start = parse_of_datetime(node.defer, tz)
        if start is not None and start > now:
            return True
    for child, parent in itertools.pairwise(chain):
        if parent.order == "sequential":
            first = next((s for s in db.children_of(parent.id) if is_active(s)), None)
            if first is not None and first.id != child.id:
                return True
    return False


def is_available(db: Database, task: OFTask, now: datetime, tz: Any) -> bool:
    """A leaf action that can be worked on right now."""
    return is_active(task) and not _container_blocks(db, task, now, tz)


@dataclass(slots=True)
class PerspectiveResult:
    """Outcome of evaluating a perspective."""

    tasks: list[OFTask] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    readable: bool = True


def _as_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [] if value is None else [str(value)]


def evaluate_perspective(
    db: Database, perspective: OFPerspective, now: datetime, tz: Any
) -> PerspectiveResult:
    """Apply a perspective's filter rules to the database's leaf actions."""
    rules = perspective.rules
    result = PerspectiveResult()
    if rules is None:
        result.readable = False
        return result
    unsupported: set[str] = set()

    def descendants(root_id: str) -> set[str]:
        out, stack = set(), [root_id]
        while stack:
            for child in db.children_of(stack.pop()):
                if child.id not in out:
                    out.add(child.id)
                    stack.append(child.id)
        return out

    def focus_ids(ids: list[str]) -> set[str]:
        found: set[str] = set()
        for oid in ids:
            if oid in db.tasks:
                found.add(oid)
                found |= descendants(oid)
            elif oid in db.folders:
                for project in db.projects(include_finished=True):
                    fid = project.project_folder_id
                    while fid:
                        if fid == oid:
                            found.add(project.id)
                            found |= descendants(project.id)
                            break
                        folder = db.folders.get(fid)
                        fid = folder.parent_id if folder else None
        return found

    def rule_matcher(rule: dict[str, Any]) -> Callable[[OFTask], bool]:
        if "aggregateType" in rule and "aggregateRules" in rule:
            return aggregate(str(rule["aggregateType"]), list(rule["aggregateRules"]))
        checks: list[Callable[[OFTask], bool]] = []
        for key, value in rule.items():
            if key == "actionAvailability":
                mode = str(value)
                if mode in ("available", "firstAvailable"):
                    checks.append(lambda t: is_available(db, t, now, tz))
                elif mode == "remaining":
                    checks.append(is_active)
                elif mode == "completed":
                    checks.append(lambda t: bool(t.completed))
                elif mode == "dropped":
                    checks.append(lambda t: bool(t.dropped))
                else:
                    unsupported.add(f"{key}={mode}")
            elif key in ("actionIsFlagged", "actionStatus") and (
                key == "actionIsFlagged" or str(value) == "flagged"
            ):
                want = bool(value) if key == "actionIsFlagged" else True
                checks.append(lambda t, w=want: t.flagged == w)
            elif key == "actionStatus" and str(value) in ("overdue", "due", "dueSoon"):
                if str(value) == "overdue":
                    checks.append(
                        lambda t: (d := parse_of_datetime(t.due, tz)) is not None and d <= now
                    )
                else:
                    checks.append(lambda t: bool(t.due))
            elif key == "actionHasAnyOfTags":
                ids = set(_as_list(value))
                checks.append(lambda t, i=ids: bool(db.tag_ids_for(t) & i))
            elif key == "actionHasAllOfTags":
                ids = set(_as_list(value))
                checks.append(lambda t, i=ids: i <= db.tag_ids_for(t))
            elif key == "actionHasNoTags":
                checks.append(lambda t, w=bool(value): (not db.tag_ids_for(t)) == w)
            elif key == "actionWithinFocus":
                scope = focus_ids(_as_list(value))
                checks.append(lambda t, s=scope: t.id in s)
            elif key == "actionIsLeaf":
                checks.append(lambda t, w=bool(value): is_leaf(db, t) == w)
            elif key == "actionHasDueDate":
                checks.append(lambda t, w=bool(value): bool(t.due) == w)
            elif key == "actionHasDeferDate":
                checks.append(lambda t, w=bool(value): bool(t.defer) == w)
            elif key == "actionIsInInbox":
                checks.append(lambda t, w=bool(value): t.in_inbox == w)
            else:
                unsupported.add(key)
        return lambda t: all(c(t) for c in checks)

    def aggregate(kind: str, sub_rules: list[dict[str, Any]]) -> Callable[[OFTask], bool]:
        matchers = [rule_matcher(r) for r in sub_rules if isinstance(r, dict)]
        if kind == "any":
            return lambda t: any(m(t) for m in matchers) if matchers else True
        if kind == "none":
            return lambda t: not any(m(t) for m in matchers)
        return lambda t: all(m(t) for m in matchers)

    top = aggregate(
        str(rules.get("archivedTopLevelFilterAggregation", "all")),
        list(rules.get("archivedFilterRules", [])),
    )
    candidates = (t for t in db.tasks.values() if not t.is_project)
    result.tasks = [t for t in candidates if is_leaf(db, t) and top(t)]
    result.unsupported = sorted(unsupported)
    return result
