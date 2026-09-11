"""Scheduled tasks: storage, next-run maths and a small polling scheduler.

Tasks live in the same SQLite file as the chat history. Three flavours:

* ``once``     - run at an absolute datetime (``2026-09-12 08:00``)
* ``interval`` - run every N minutes
* ``daily``    - run once a day at HH:MM

The scheduler only decides *when*; what to do is injected as a coroutine
runner, so the same code drives both the TUI and the headless daemon.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Awaitable, Callable

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL DEFAULT '',
    kind            TEXT NOT NULL,
    spec            TEXT NOT NULL,
    prompt          TEXT NOT NULL,
    allow_dangerous INTEGER NOT NULL DEFAULT 0,
    enabled         INTEGER NOT NULL DEFAULT 1,
    last_run        TEXT NOT NULL DEFAULT '',
    next_run        TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);
"""

TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d",
)

SCHEDULE_RE = re.compile(r"@(?P<kind>every|daily|once)\s+(?P<spec>.+?)\s*$", re.IGNORECASE)
INTERVAL_RE = re.compile(r"^(?P<value>\d+)\s*(?P<unit>m|min|mins|minute|minutes|h|hour|hours)?$", re.IGNORECASE)
CLOCK_RE = re.compile(r"^(?P<hour>\d{1,2}):(?P<minute>\d{1,2})$")

DANGER_FLAG = "--danger"


class ScheduleError(ValueError):
    """Raised when a schedule expression cannot be understood."""


def parse_datetime(text: str) -> datetime:
    cleaned = text.strip().replace("T", " ").replace("/", "-")
    if cleaned.count(":") == 1 and len(cleaned) > 16:
        cleaned += ":00"
    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    raise ScheduleError(f"无法解析时间 '{text}'，请用 2026-09-12 08:00 这种格式。")


def parse_interval(text: str) -> int:
    match = INTERVAL_RE.match(text.strip())
    if not match:
        raise ScheduleError(f"无法解析间隔 '{text}'，请用 30m / 2h 这种格式。")
    value = int(match.group("value"))
    unit = (match.group("unit") or "m").lower()
    minutes = value * 60 if unit.startswith("h") else value
    if minutes <= 0:
        raise ScheduleError("间隔必须大于 0 分钟。")
    return minutes


def parse_clock(text: str) -> tuple[int, int]:
    match = CLOCK_RE.match(text.strip())
    if not match:
        raise ScheduleError(f"无法解析时间点 '{text}'，请用 09:30 这种格式。")
    hour, minute = int(match.group("hour")), int(match.group("minute"))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ScheduleError(f"时间点 '{text}' 超出范围。")
    return hour, minute


def describe(kind: str, spec: str) -> str:
    if kind == "interval":
        minutes = int(spec or 0)
        return f"每 {minutes} 分钟" if minutes % 60 else f"每 {minutes // 60} 小时"
    if kind == "daily":
        return f"每天 {spec}"
    if kind == "once":
        return f"一次 · {spec}"
    return spec


@dataclass
class ScheduledTask:
    id: int | None
    kind: str
    spec: str
    prompt: str
    name: str = ""
    allow_dangerous: bool = False
    enabled: bool = True
    last_run: str = ""
    next_run: str = ""

    @property
    def title(self) -> str:
        return self.name or self.prompt[:40]

    @property
    def schedule_text(self) -> str:
        return describe(self.kind, self.spec)

    def next_after(self, after: datetime) -> datetime | None:
        """When should this task fire next, given a reference point?"""

        if self.kind == "once":
            when = parse_datetime(self.spec)
            return when if when > after else None
        if self.kind == "interval":
            minutes = int(self.spec)
            base = parse_datetime(self.next_run) if self.next_run else after
            nxt = base + timedelta(minutes=minutes)
            # Never storm through missed runs - skip to the next slot from now.
            if nxt <= after:
                nxt = after + timedelta(minutes=minutes)
            return nxt
        if self.kind == "daily":
            hour, minute = parse_clock(self.spec)
            nxt = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if nxt <= after:
                nxt += timedelta(days=1)
            return nxt
        raise ScheduleError(f"未知的任务类型 '{self.kind}'")


class TaskStore:
    """CRUD for scheduled tasks, on the shared history connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def add(self, task: ScheduledTask, now: datetime | None = None) -> ScheduledTask:
        now = now or datetime.now()
        nxt = task.next_after(now)
        cursor = self.conn.execute(
            "INSERT INTO tasks (name, kind, spec, prompt, allow_dangerous, enabled, next_run, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task.name or task.prompt[:40],
                task.kind,
                task.spec,
                task.prompt,
                1 if task.allow_dangerous else 0,
                1 if task.enabled else 0,
                nxt.isoformat(timespec="seconds") if nxt else "",
                now.isoformat(timespec="seconds"),
            ),
        )
        self.conn.commit()
        task.id = int(cursor.lastrowid or 0)
        task.next_run = nxt.isoformat(timespec="seconds") if nxt else ""
        return task

    def list(self) -> list[ScheduledTask]:
        rows = self.conn.execute(
            "SELECT id, name, kind, spec, prompt, allow_dangerous, enabled, last_run, next_run"
            " FROM tasks ORDER BY id"
        ).fetchall()
        return [
            ScheduledTask(
                id=int(row[0]),
                name=row[1],
                kind=row[2],
                spec=row[3],
                prompt=row[4],
                allow_dangerous=bool(row[5]),
                enabled=bool(row[6]),
                last_run=row[7],
                next_run=row[8],
            )
            for row in rows
        ]

    def get(self, task_id: int) -> ScheduledTask | None:
        return next((t for t in self.list() if t.id == task_id), None)

    def delete(self, task_id: int) -> bool:
        cursor = self.conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        self.conn.commit()
        return cursor.rowcount > 0

    def set_enabled(self, task_id: int, enabled: bool) -> bool:
        cursor = self.conn.execute(
            "UPDATE tasks SET enabled = ? WHERE id = ?", (1 if enabled else 0, task_id)
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def mark_run(self, task: ScheduledTask, when: datetime) -> None:
        nxt = task.next_after(when)
        task.last_run = when.isoformat(timespec="seconds")
        task.next_run = nxt.isoformat(timespec="seconds") if nxt else ""
        self.conn.execute(
            "UPDATE tasks SET last_run = ?, next_run = ?, enabled = ? WHERE id = ?",
            (task.last_run, task.next_run, 0 if nxt is None else int(task.enabled), task.id),
        )
        self.conn.commit()

    def due(self, now: datetime | None = None) -> list[ScheduledTask]:
        now = now or datetime.now()
        pending = []
        for task in self.list():
            if not task.enabled or not task.next_run:
                continue
            try:
                if parse_datetime(task.next_run) <= now:
                    pending.append(task)
            except ScheduleError:
                continue
        return pending


def parse_task_command(text: str) -> ScheduledTask:
    """Parse ``/task add`` input: ``<prompt> @every 30m [--danger]``."""

    raw = text.strip()
    allow_dangerous = DANGER_FLAG in raw
    raw = raw.replace(DANGER_FLAG, " ").strip()

    match = SCHEDULE_RE.search(raw)
    if not match:
        raise ScheduleError(
            "缺少调度表达式。例如：/task add 检查磁盘空间 @daily 09:00"
            "（支持 @every 30m / @daily 09:00 / @once 2026-09-12 08:00）"
        )
    kind = match.group("kind").lower()
    spec = match.group("spec").strip()
    prompt = raw[: match.start()].strip(" -|,")
    if not prompt:
        raise ScheduleError("缺少任务内容，例如：/task add 汇总今天的错误日志 @daily 18:00")

    if kind == "every":
        spec = str(parse_interval(spec))
        kind = "interval"
    elif kind == "daily":
        hour, minute = parse_clock(spec)
        spec = f"{hour:02d}:{minute:02d}"
    else:
        spec = parse_datetime(spec).strftime("%Y-%m-%d %H:%M")

    return ScheduledTask(
        id=None, kind=kind, spec=spec, prompt=prompt, allow_dangerous=allow_dangerous
    )


TaskRunner = Callable[[ScheduledTask], Awaitable[None]]


@dataclass
class Scheduler:
    """Polls the task table and hands due tasks to a runner callback."""

    store: TaskStore
    runner: TaskRunner
    check_interval: float = 20.0
    _task: asyncio.Task | None = field(default=None, repr=False)
    on_error: Callable[[ScheduledTask, str], None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="jarvis-scheduler")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.check_interval)
                await self.run_due_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must survive anything
                continue

    async def run_due_once(self, now: datetime | None = None) -> list[ScheduledTask]:
        """Fire every task that is due. Testable without waiting."""

        now = now or datetime.now()
        fired: list[ScheduledTask] = []
        for task in self.store.due(now):
            self.store.mark_run(task, now)
            fired.append(task)
            try:
                await self.runner(task)
            except Exception as exc:  # noqa: BLE001
                if self.on_error is not None:
                    self.on_error(task, f"{type(exc).__name__}: {exc}")
        return fired
