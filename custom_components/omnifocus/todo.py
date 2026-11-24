"""To-do list platform: one entity per mapped OmniFocus target."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .api import (
    ItemSnapshot,
    OmniFocusError,
    OmniFocusNotFoundError,
    OmniFocusReadOnlyError,
    Target,
    TargetResult,
)
from .const import (
    CONF_MODE,
    CONF_NAME,
    DEFAULT_DUE_HOUR,
    DOMAIN,
    MODE_READ_WRITE,
    TARGET_PERSPECTIVE,
)
from .coordinator import (
    OmniFocusConfigEntry,
    OmniFocusCoordinator,
    list_subentries,
    target_for_subentry,
)

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OmniFocusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create a to-do list entity for each mapping subentry."""
    for subentry_id, subentry in list_subentries(entry).items():
        async_add_entities(
            [OmniFocusTodoList(entry.runtime_data, entry, subentry)],
            config_subentry_id=subentry_id,
        )


def _to_local_due(due: date | datetime) -> datetime:
    """HA due -> local wall-clock datetime (OmniFocus due dates are floating)."""
    if isinstance(due, datetime):
        return dt_util.as_local(due)
    return datetime.combine(due, time(DEFAULT_DUE_HOUR), tzinfo=dt_util.get_default_time_zone())


class OmniFocusTodoList(CoordinatorEntity[OmniFocusCoordinator], TodoListEntity):
    """An HA to-do list backed by an OmniFocus inbox, project or perspective."""

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(
        self,
        coordinator: OmniFocusCoordinator,
        entry: OmniFocusConfigEntry,
        subentry: ConfigSubentry,
    ) -> None:
        super().__init__(coordinator)
        self._target: Target = target_for_subentry(subentry)
        self._writable = subentry.data.get(CONF_MODE) == MODE_READ_WRITE
        self._attr_unique_id = subentry.subentry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, subentry.subentry_id)},
            name=subentry.data[CONF_NAME],
            manufacturer="The Omni Group",
            model=f"OmniFocus {self._target.kind}",
            entry_type=None,
        )
        features = TodoListEntityFeature(0)
        if self._writable:
            features |= (
                TodoListEntityFeature.UPDATE_TODO_ITEM
                | TodoListEntityFeature.DELETE_TODO_ITEM
                | TodoListEntityFeature.SET_DUE_DATE_ON_ITEM
                | TodoListEntityFeature.SET_DUE_DATETIME_ON_ITEM
                | TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
            )
            if self._target.kind != TARGET_PERSPECTIVE:
                features |= TodoListEntityFeature.CREATE_TODO_ITEM
        self._attr_supported_features = features

    # ---- state -----------------------------------------------------------------

    @property
    def _result(self) -> TargetResult | None:
        data = self.coordinator.data
        return data.results.get(self._target) if data else None

    @property
    def available(self) -> bool:
        result = self._result
        return super().available and result is not None and result.found and result.readable

    @property
    def todo_items(self) -> list[TodoItem] | None:
        result = self._result
        if result is None:
            return None
        return [
            TodoItem(
                uid=i.id,
                summary=i.name,
                status=TodoItemStatus.COMPLETED if i.completed else TodoItemStatus.NEEDS_ACTION,
                due=i.due,
                description=i.note,
            )
            for i in result.items
        ]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        result = self._result
        attrs: dict[str, Any] = {
            "omnifocus_target": self._target.kind,
            "mode": MODE_READ_WRITE if self._writable else "read_only",
        }
        if result and result.unsupported_rules:
            attrs["unsupported_perspective_rules"] = result.unsupported_rules
        return attrs

    def _item(self, uid: str) -> ItemSnapshot | None:
        result = self._result
        return next((i for i in result.items if i.id == uid), None) if result else None

    # ---- writes ----------------------------------------------------------------

    async def _write(self, call) -> None:
        if not self._writable:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="read_only")
        try:
            await call()
        except OmniFocusReadOnlyError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="read_only"
            ) from err
        except OmniFocusNotFoundError as err:
            await self.coordinator.async_request_refresh()
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="not_found",
            ) from err
        except OmniFocusError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="write_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        await self.coordinator.async_refresh()

    async def async_create_todo_item(self, item: TodoItem) -> None:
        await self._write(
            lambda: self.coordinator.client.async_add_task(
                self._target,
                item.summary or "",
                note=item.description or None,
                due=_to_local_due(item.due) if item.due else None,
            )
        )

    async def async_update_todo_item(self, item: TodoItem) -> None:
        current = self._item(item.uid or "")
        if current is None:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="not_found")
        kwargs: dict[str, Any] = {}
        if item.summary is not None and item.summary != current.name:
            kwargs["name"] = item.summary
        if (item.description or None) != current.note:
            kwargs["note"] = item.description or None
        new_due = _to_local_due(item.due) if item.due else None
        if new_due != current.due:
            kwargs["due"] = new_due
        if item.status is not None:
            done = item.status == TodoItemStatus.COMPLETED
            if done != current.completed:
                kwargs["completed"] = done
        if not kwargs:
            return
        await self._write(lambda: self.coordinator.client.async_update_task(current.id, **kwargs))

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        await self._write(lambda: self.coordinator.client.async_delete_tasks(uids))
