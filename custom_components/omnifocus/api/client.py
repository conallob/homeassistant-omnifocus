"""High level OmniFocus client: sync, snapshot and write operations."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any, Literal, TypeVar
from urllib.parse import quote

import aiohttp

from .database import Database, parse_of_datetime
from .errors import (
    OmniFocusError,
    OmniFocusNotFoundError,
    OmniFocusReadOnlyError,
)
from .model import OFTask
from .perspective import evaluate_perspective, is_active, is_leaf
from .store import OmniFocusStore, compute_head_tail
from .webdav import WebDavClient
from .writer import (
    ClientInfo,
    build_add_task,
    build_client_plist,
    build_delete_task,
    build_update_task,
)

_LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")

DEFAULT_SERVER = "https://sync.omnigroup.com"
DEFAULT_DATABASE = "OmniFocus.ofocus"
# How long completed tasks stay visible in a list, so a just-ticked item does
# not vanish instantly and can be un-ticked.
RECENTLY_COMPLETED = timedelta(days=7)

Executor = Callable[..., Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class Target:
    """What an HA to-do list is mapped to."""

    kind: Literal["inbox", "project", "perspective"]
    ref: str | None = None


@dataclass(slots=True)
class ItemSnapshot:
    """A task as presented to Home Assistant."""

    id: str
    name: str
    note: str | None
    due: datetime | None
    completed: bool
    repeating: bool


@dataclass(slots=True)
class TargetResult:
    """Items for one mapped target."""

    items: list[ItemSnapshot] = field(default_factory=list)
    found: bool = True
    readable: bool = True
    unsupported_rules: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Snapshot:
    """Everything the coordinator needs after one sync."""

    projects: dict[str, str] = field(default_factory=dict)  # id -> label
    perspectives: dict[str, str] = field(default_factory=dict)  # id -> name
    results: dict[Target, TargetResult] = field(default_factory=dict)


def _order_key(db: Database, task: OFTask) -> tuple[tuple[bool, int], ...]:
    chain = [task, *db.ancestors(task)]
    return tuple((t.rank is None, t.rank or 0) for t in reversed(chain))


def build_snapshot(db: Database, targets: list[Target], tz: tzinfo, now: datetime) -> Snapshot:
    """Compute catalog and per-target items from a database (CPU bound)."""
    snap = Snapshot()
    for project in db.projects():
        folder = db.folder_path(project.project_folder_id)
        snap.projects[project.id] = f"{folder} / {project.name}" if folder else project.name
    for pid, persp in db.perspectives.items():
        if persp.name:
            snap.perspectives[pid] = persp.name

    cutoff = now - RECENTLY_COMPLETED

    def visible(task: OFTask) -> bool:
        if task.dropped or task.project_status == "dropped":
            return False
        if is_active(task):
            return True
        done = parse_of_datetime(task.completed, UTC)
        return done is not None and done >= cutoff

    def item(task: OFTask) -> ItemSnapshot:
        return ItemSnapshot(
            id=task.id,
            name=task.name,
            note=task.note,
            due=parse_of_datetime(task.due, tz),
            completed=bool(task.completed),
            repeating=bool(task.repetition_rule),
        )

    for target in targets:
        result = TargetResult()
        tasks: list[OFTask] = []
        if target.kind == "inbox":
            tasks = [t for t in db.tasks.values() if t.in_inbox and not t.is_project]
        elif target.kind == "project":
            project = db.tasks.get(target.ref or "")
            if project is None or not project.is_project:
                result.found = False
            else:
                tasks = [
                    t
                    for t in db.tasks.values()
                    if not t.is_project
                    and is_leaf(db, t)
                    and (owner := db.project_of(t)) is not None
                    and owner.id == project.id
                ]
        else:
            persp = db.perspectives.get(target.ref or "")
            if persp is None:
                result.found = False
            else:
                evaluated = evaluate_perspective(db, persp, now, tz)
                result.readable = evaluated.readable
                result.unsupported_rules = evaluated.unsupported
                tasks = evaluated.tasks
        tasks = sorted((t for t in tasks if visible(t)), key=lambda t: _order_key(db, t))
        result.items = [item(t) for t in tasks]
        snap.results[target] = result
    return snap


class OmniFocusClient:
    """Sync client for one OmniFocus account."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        username: str,
        password: str,
        passphrase: str | None,
        run_in_executor: Executor,
        client_id: str,
        host_id: str,
        client_name: str,
        registration_date: datetime,
        base_url: str = DEFAULT_SERVER,
        database: str = DEFAULT_DATABASE,
        read_only: bool = True,
    ) -> None:
        self._executor = run_in_executor
        self._passphrase = passphrase or password
        self._client_id = client_id
        self._host_id = host_id
        self._client_name = client_name
        self._registration_date = registration_date
        self.read_only = read_only
        url = f"{base_url.rstrip('/')}/{quote(username, safe='')}/{database}"
        self._store = OmniFocusStore(WebDavClient(session, username, password), url)
        self._lock = asyncio.Lock()
        self._unlocked = False
        self._db = Database()
        self._applied: set[str] = set()
        self._head_tail: str | None = None
        self._client_files: list[str] = []

    # ---- syncing ---------------------------------------------------------------

    async def _ensure_unlocked(self) -> None:
        if not self._unlocked:
            await self._store.unlock(self._passphrase, self._executor)
            self._unlocked = True

    async def async_validate(self) -> None:
        """Connect, authenticate and unlock; raises on any problem."""
        async with self._lock:
            await self._ensure_unlocked()
            await self._store.list()

    async def async_encryption_info(self) -> dict[str, object] | None:
        """Non-secret encryption settings (never keys or salts), for diagnostics."""
        async with self._lock:
            return await self._store.encryption_info()

    async def _fetch_new(self) -> list[tuple[str, bytes]]:
        """List the store and download transactions we have not applied yet."""
        listing = await self._store.list()
        names = {t.name for t in listing.transactions}
        if self._applied and not self._applied <= names:
            _LOGGER.debug("Store was compacted or reset; rebuilding from scratch")
            self._db = Database()
            self._applied = set()
        self._client_files = listing.client_files
        self._head_tail = compute_head_tail(listing.transactions)
        return [
            (t.name, await self._store.read_contents_raw(t.name))
            for t in listing.transactions
            if t.name not in self._applied
        ]

    def _apply_raw(self, raws: list[tuple[str, bytes]]) -> None:
        for name, raw in raws:
            self._db.apply_transaction(self._store.contents_from_raw(name, raw))
            self._applied.add(name)

    async def async_refresh(
        self, targets: list[Target], tz: tzinfo, now: datetime | None = None
    ) -> Snapshot:
        """Pull new transactions and compute a snapshot for ``targets``."""
        async with self._lock:
            await self._ensure_unlocked()
            raws = await self._fetch_new()
            when = now or datetime.now(UTC)

            def work() -> Snapshot:
                self._apply_raw(raws)
                return build_snapshot(self._db, targets, tz, when)

            return await self._executor(work)

    # ---- writing ---------------------------------------------------------------

    def _assert_writable(self) -> None:
        if self.read_only:
            raise OmniFocusReadOnlyError("OmniFocus is configured read-only")

    async def _commit(self, contents_xml: str) -> None:
        """Append a transaction and refresh our client registration."""
        parents = [self._head_tail] if self._head_tail else []
        name, tail = await self._store.write_transaction(contents_xml, parents)
        await self._executor(self._db.apply_transaction, contents_xml)
        self._applied.add(name)
        self._head_tail = tail
        plist = build_client_plist(
            ClientInfo(
                client_id=self._client_id,
                host_id=self._host_id,
                name=self._client_name,
                registration_date=self._registration_date,
                last_sync_date=datetime.now(UTC),
                tail_identifiers=[tail],
            )
        )
        new_client = await self._store.write_client_file(self._client_id, plist)
        # Keep exactly one registration file for this client.
        suffix = f"={self._client_id}.client"
        for old in self._client_files:
            if old.endswith(suffix) and old != new_client:
                try:
                    await self._store.delete(old)
                except OmniFocusError:
                    _LOGGER.debug("Could not prune old client file %s", old)
        self._client_files = [new_client]

    async def _prepare_write(self) -> None:
        """Bring local state up to date before building a transaction."""
        await self._ensure_unlocked()
        raws = await self._fetch_new()
        await self._executor(self._apply_raw, raws)

    def _require_task(self, task_id: str) -> OFTask:
        task = self._db.tasks.get(task_id)
        if task is None:
            raise OmniFocusNotFoundError(f"Task {task_id} no longer exists in OmniFocus")
        return task

    async def async_add_task(
        self,
        target: Target,
        name: str,
        *,
        note: str | None = None,
        due: datetime | None = None,
    ) -> str:
        """Create a task in the inbox or a project; returns its id."""
        self._assert_writable()
        if target.kind == "perspective":
            raise OmniFocusError("Items cannot be added to a perspective")
        async with self._lock:
            await self._prepare_write()
            parent: str | None = None
            if target.kind == "project":
                project = self._db.tasks.get(target.ref or "")
                if project is None or not project.is_project:
                    raise OmniFocusNotFoundError("The mapped OmniFocus project no longer exists")
                parent = project.id
            xml, task_id = build_add_task(
                name,
                note=note,
                due=due.replace(tzinfo=None) if due else None,
                parent_id=parent,
            )
            await self._commit(xml)
            return task_id

    async def async_update_task(
        self,
        task_id: str,
        *,
        name: str | None = None,
        note: str | Any | None = ...,
        due: datetime | Any | None = ...,
        completed: bool | None = None,
    ) -> None:
        """Edit a task. ``...`` leaves a field unchanged; ``None`` clears it."""
        self._assert_writable()
        async with self._lock:
            await self._prepare_write()
            task = self._require_task(task_id)
            fields: dict[str, Any] = {}
            if note is not ...:
                fields["note"] = note
            if due is not ...:
                fields["due"] = due.replace(tzinfo=None) if isinstance(due, datetime) else None
            if completed is not None and bool(task.completed) != completed:
                if completed and task.repetition_rule:
                    raise OmniFocusError(
                        "Repeating tasks cannot be completed from Home Assistant; "
                        "complete them in OmniFocus so the next occurrence is created"
                    )
                fields["completed"] = datetime.now(UTC) if completed else None
            if name is None and not fields:
                return
            await self._commit(build_update_task(task_id, added=task.added, name=name, **fields))

    async def async_delete_tasks(self, task_ids: list[str]) -> None:
        """Delete tasks by id."""
        self._assert_writable()
        async with self._lock:
            await self._prepare_write()
            for task_id in task_ids:
                self._require_task(task_id)
                await self._commit(build_delete_task(task_id))
