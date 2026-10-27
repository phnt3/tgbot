"""Tests for the persistent task scheduler.

Storage always points at ``tmp_path`` and execution uses an injected fake, so no
power action is ever taken. The clock is faked for deterministic timing.
"""

import asyncio
import json

import pytest

from scheduler import TaskScheduler, describe_task


class Clock:
    def __init__(self, value=1_000_000.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class FakeBot:
    def __init__(self, fail=False):
        self.fail = fail
        self.sent = []

    async def send_message(self, chat_id, text):
        if self.fail:
            raise RuntimeError("blocked")
        self.sent.append((chat_id, text))


def make_scheduler(tmp_path, clock=None, executor=None, bot=None):
    return TaskScheduler(
        tmp_path / "tasks.json",
        bot=bot,
        user_ids=[1, 2],
        executor=executor,
        clock=clock or Clock(),
    )


def test_add_persists_and_reloads(tmp_path):
    clock = Clock()
    scheduler = make_scheduler(tmp_path, clock)
    task = scheduler.add("shutdown", delay_seconds=600)

    assert task["id"] == 1
    assert task["action"] == "shutdown"
    assert task["run_at"] == clock.value + 600
    assert scheduler.list_tasks() == [task]

    reloaded = make_scheduler(tmp_path, clock)
    assert reloaded.list_tasks() == [task]
    assert reloaded.add("lock", delay_seconds=10)["id"] == 2


def test_add_rejects_invalid_requests(tmp_path):
    scheduler = make_scheduler(tmp_path)

    with pytest.raises(ValueError):
        scheduler.add("format_disk", delay_seconds=10)
    with pytest.raises(ValueError):
        scheduler.add("shutdown")
    with pytest.raises(ValueError):
        scheduler.add("shutdown", delay_seconds=10, run_at=100)
    with pytest.raises(ValueError):
        scheduler.add("shutdown", delay_seconds=0)
    with pytest.raises(ValueError):
        scheduler.add("shutdown", run_at=1)
    with pytest.raises(ValueError):
        scheduler.add("profile", delay_seconds=10)


def test_profile_task_keeps_its_parameter(tmp_path):
    scheduler = make_scheduler(tmp_path)
    task = scheduler.add(
        "profile", delay_seconds=60, params={"profile": "GAMING"}
    )

    assert task["params"] == {"profile": "GAMING"}
    assert "GAMING" in describe_task(task)


def test_cancel_and_cancel_all(tmp_path):
    scheduler = make_scheduler(tmp_path)
    first = scheduler.add("shutdown", delay_seconds=60)
    second = scheduler.add("lock", delay_seconds=120)

    assert scheduler.cancel(999) is False
    assert scheduler.cancel(first["id"]) is True
    assert [task["id"] for task in scheduler.list_tasks()] == [second["id"]]

    remaining = make_scheduler(tmp_path)
    assert len(remaining.list_tasks()) == 1

    assert remaining.cancel_all() == 1
    assert remaining.list_tasks() == []
    assert make_scheduler(tmp_path).list_tasks() == []


def test_run_due_executes_and_removes_tasks(tmp_path):
    clock = Clock()
    executed = []

    def executor(task):
        executed.append(task["action"])
        return True, "ok"

    bot = FakeBot()
    scheduler = make_scheduler(tmp_path, clock, executor=executor, bot=bot)
    due = scheduler.add("lock", delay_seconds=10)
    future = scheduler.add("shutdown", delay_seconds=600)

    clock.advance(11)
    results = asyncio.run(scheduler.run_due())

    assert executed == ["lock"]
    assert len(results) == 1
    assert results[0]["task"]["id"] == due["id"]
    assert results[0]["ok"] is True
    assert scheduler.list_tasks() == [future]
    assert bot.sent and bot.sent[0][0] == 1
    assert "DONE" in bot.sent[0][1]


def test_run_due_reports_executor_errors(tmp_path):
    clock = Clock()

    def executor(task):
        raise RuntimeError("boom")

    bot = FakeBot()
    scheduler = make_scheduler(tmp_path, clock, executor=executor, bot=bot)
    scheduler.add("shutdown", delay_seconds=1)
    clock.advance(2)

    results = asyncio.run(scheduler.run_due())

    assert results[0]["ok"] is False
    assert "boom" in results[0]["message"]
    assert "FAILED" in bot.sent[0][1]
    assert scheduler.list_tasks() == []


def test_run_due_notifies_both_users(tmp_path):
    clock = Clock()
    bot = FakeBot()
    scheduler = make_scheduler(
        tmp_path, clock, executor=lambda task: (True, "done"), bot=bot
    )
    scheduler.add("sleep", delay_seconds=1)
    clock.advance(2)

    asyncio.run(scheduler.run_due())

    assert [chat for chat, _ in bot.sent] == [1, 2]


def test_stale_and_malformed_tasks_are_dropped(tmp_path):
    clock = Clock(10_000.0)
    storage = tmp_path / "tasks.json"
    storage.write_text(
        json.dumps(
            [
                {"id": 1, "action": "shutdown", "run_at": 10_000},
                {"id": 2, "action": "shutdown", "run_at": 9_000},
                {"id": 3, "action": "rm_rf", "run_at": 20_000},
                {"id": 1, "action": "lock", "run_at": 20_000},
                {"id": 5, "action": "lock", "run_at": 20_000},
                "not a task",
            ]
        ),
        encoding="utf-8",
    )

    scheduler = make_scheduler(tmp_path, clock)

    assert [task["id"] for task in scheduler.list_tasks()] == [1, 5]
    saved = json.loads(storage.read_text(encoding="utf-8"))
    assert [task["id"] for task in saved] == [1, 5]


def test_corrupt_storage_file_is_ignored(tmp_path):
    storage = tmp_path / "tasks.json"
    storage.write_text("{not json", encoding="utf-8")

    scheduler = make_scheduler(tmp_path)

    assert scheduler.list_tasks() == []
    assert scheduler.add("lock", delay_seconds=10)["id"] == 1


def test_exactly_one_overdue_task_within_grace_is_kept(tmp_path):
    clock = Clock(10_000.0)
    storage = tmp_path / "tasks.json"
    storage.write_text(
        json.dumps(
            [{"id": 1, "action": "shutdown", "run_at": 10_000.0 - 119}]
        ),
        encoding="utf-8",
    )

    scheduler = make_scheduler(tmp_path, clock)

    assert len(scheduler.list_tasks()) == 1


def test_start_and_stop_are_safe(tmp_path):
    scheduler = make_scheduler(tmp_path)

    async def scenario():
        task = scheduler.start()
        assert scheduler.start() is task
        await scheduler.stop()
        assert task.cancelled() or task.done()
        # Stopping again is a no-op.
        await scheduler.stop()

    asyncio.run(scenario())