"""Perspective and availability evaluation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.omnifocus.api.client import Target, build_snapshot
from custom_components.omnifocus.api.database import Database
from tests.fake_server import contents, task_xml

NOW = datetime(2026, 7, 1, 12, tzinfo=UTC)


def persp(pid: str, name: str, rules_xml: str) -> str:
    return (
        f'<perspective id="{pid}"><plist version="1.0"><dict>'
        f"<key>name</key><string>{name}</string>{rules_xml}</dict></plist></perspective>"
    )


def build() -> Database:
    future = (NOW + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S.000")
    db = Database()
    db.apply_transaction(
        contents(
            '<context id="errand"><name>errand</name></context>',
            task_xml("pr", "Proj", project="active", order="sequential"),
            task_xml("a", "First", parent="pr", rank=1),
            task_xml("b", "Second (blocked)", parent="pr", rank=2),
            task_xml("s", "Standalone", project="active"),
            task_xml("c", "Flagged one", parent="s", rank=1, flagged=True),
            task_xml("d", "Deferred", parent="s", rank=2),
            '<task id="d" op="update"><start>' + future + "</start></task>",
            '<task-to-tag id="c.errand"><task idref="c"/><context idref="errand"/></task-to-tag>',
            persp(
                "pAvail",
                "Available",
                "<key>archivedFilterRules</key><array><dict>"
                "<key>actionAvailability</key><string>available</string></dict></array>"
                "<key>archivedTopLevelFilterAggregation</key><string>all</string>",
            ),
            persp(
                "pTag",
                "Errands",
                "<key>archivedFilterRules</key><array><dict>"
                "<key>actionHasAnyOfTags</key><array><string>errand</string></array>"
                "</dict></array>",
            ),
            persp(
                "pUnknown",
                "Weird",
                "<key>archivedFilterRules</key><array><dict>"
                "<key>actionMadeUpRule</key><true/></dict></array>",
            ),
        )
    )
    return db


def names(db: Database, pid: str) -> list[str]:
    snap = build_snapshot(db, [Target("perspective", pid)], UTC, NOW)
    return [i.name for i in snap.results[Target("perspective", pid)].items]


def test_available_respects_sequential_and_defer() -> None:
    assert sorted(names(build(), "pAvail")) == ["First", "Flagged one"]


def test_tag_rule() -> None:
    assert names(build(), "pTag") == ["Flagged one"]


def test_unknown_rule_reported_not_hidden() -> None:
    db = build()
    snap = build_snapshot(db, [Target("perspective", "pUnknown")], UTC, NOW)
    result = snap.results[Target("perspective", "pUnknown")]
    assert result.unsupported_rules == ["actionMadeUpRule"]


def test_missing_perspective() -> None:
    snap = build_snapshot(build(), [Target("perspective", "gone")], UTC, NOW)
    assert snap.results[Target("perspective", "gone")].found is False


def test_catalog_lists_perspectives_and_projects() -> None:
    snap = build_snapshot(build(), [], UTC, NOW)
    assert snap.perspectives["pTag"] == "Errands"
    assert set(snap.projects) == {"pr", "s"}
