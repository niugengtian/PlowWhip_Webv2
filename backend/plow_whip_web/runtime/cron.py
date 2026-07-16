from __future__ import annotations

import os
import socket
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from plow_whip_web.runtime.scheduler import SchedulerService
from plow_whip_web.store.scheduler_repository import SchedulerRepository
from plow_whip_web.store.settings_repository import SettingsRepository


_LIMITS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


@dataclass(frozen=True, slots=True)
class CronExpression:
    source: str
    fields: tuple[frozenset[int], ...]

    @classmethod
    def parse(cls, source: str) -> "CronExpression":
        parts = source.strip().split()
        if len(parts) != 5:
            raise ValueError("cron expression must have five fields")
        expanded = [
            _expand_field(part, minimum, maximum)
            for part, (minimum, maximum) in zip(parts, _LIMITS, strict=True)
        ]
        if 7 in expanded[4]:
            expanded[4].remove(7)
            expanded[4].add(0)
        fields = tuple(frozenset(values) for values in expanded)
        return cls(" ".join(parts), fields)

    def matches(self, moment: datetime) -> bool:
        values = (moment.minute, moment.hour, moment.day, moment.month, (moment.weekday() + 1) % 7)
        minute, hour, day, month, weekday = (
            value in allowed for value, allowed in zip(values, self.fields, strict=True)
        )
        day_of_month_is_wildcard = len(self.fields[2]) == 31
        day_of_week_is_wildcard = len(self.fields[4]) == 7
        if day_of_month_is_wildcard:
            day_matches = weekday
        elif day_of_week_is_wildcard:
            day_matches = day
        else:
            day_matches = day or weekday
        return minute and hour and month and day_matches

    def next_after(self, moment: datetime, *, limit_minutes: int = 527_040) -> datetime | None:
        candidate = moment.replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(limit_minutes):
            if self.matches(candidate):
                return candidate
            candidate += timedelta(minutes=1)
        return None


def validate_timezone(name: str) -> str:
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f"unknown timezone: {name}") from error
    return name


class EmbeddedCronRunner:
    """Container-friendly, database-configured cron runner with a zero-token control path."""

    def __init__(
        self,
        service: SchedulerService,
        scheduler: SchedulerRepository,
        settings: SettingsRepository,
        *,
        poll_seconds: float = 5.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.service = service
        self.scheduler = scheduler
        self.settings = settings
        self.poll_seconds = max(1.0, poll_seconds)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.runner_id = f"{socket.gethostname()}:{os.getpid()}"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.scheduler.runner_started(self.runner_id)
        self._thread = threading.Thread(target=self.run_forever, name="plow-whip-embedded-cron", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
        self.scheduler.runner_stopped(self.runner_id)

    def run_forever(self) -> None:
        next_heartbeat = 0.0
        while not self._stop.is_set():
            try:
                now = self.clock()
                self.run_due(now)
                if monotonic() >= next_heartbeat:
                    self.scheduler.runner_heartbeat(self.runner_id)
                    next_heartbeat = monotonic() + 15
            except Exception as error:  # runner must survive one malformed tick
                self.scheduler.runner_error(self.runner_id, f"{type(error).__name__}: {error}")
            self._stop.wait(self.poll_seconds)

    def run_due(self, now: datetime) -> dict[str, object] | None:
        values = self.settings.get()["values"]
        if not values["cron_enabled"]:
            return None
        zone = ZoneInfo(values["cron_timezone"])
        local_now = now.astimezone(zone).replace(second=0, microsecond=0)
        expression = CronExpression.parse(values["cron_expression"])
        due = expression.matches(local_now)
        if not due and values["cron_misfire_policy"] == "catch_up_once":
            status = self.scheduler.status()
            last_tick = _parse_sqlite_timestamp(status["last_tick_at"])
            due = last_tick is not None and _has_missed_run(expression, last_tick.astimezone(zone), local_now)
        slot = local_now.isoformat()
        if not due or not self.scheduler.claim_cron_slot(slot):
            return None
        return self.service.tick(owner=f"embedded-cron:{self.runner_id}")


def schedule_view(values: dict[str, object], *, now: datetime | None = None) -> dict[str, object]:
    zone_name = str(values["cron_timezone"])
    zone = ZoneInfo(zone_name)
    local_now = (now or datetime.now(timezone.utc)).astimezone(zone)
    expression = CronExpression.parse(str(values["cron_expression"]))
    next_run = expression.next_after(local_now) if bool(values["cron_enabled"]) else None
    return {
        "enabled": bool(values["cron_enabled"]),
        "expression": expression.source,
        "timezone": zone_name,
        "misfire_policy": values["cron_misfire_policy"],
        "next_run_at": next_run.isoformat() if next_run else None,
    }


def _expand_field(source: str, minimum: int, maximum: int) -> set[int]:
    values: set[int] = set()
    for item in source.split(","):
        base, separator, step_source = item.partition("/")
        step = _parse_int(step_source, "step") if separator else 1
        if step < 1:
            raise ValueError("cron step must be positive")
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            start_source, end_source = base.split("-", 1)
            start, end = _parse_int(start_source, "range"), _parse_int(end_source, "range")
        else:
            start = end = _parse_int(base, "value")
        if start < minimum or end > maximum or start > end:
            raise ValueError(f"cron value outside {minimum}-{maximum}: {item}")
        values.update(range(start, end + 1, step))
    if not values:
        raise ValueError("empty cron field")
    return values


def _parse_int(source: str, label: str) -> int:
    try:
        return int(source)
    except ValueError as error:
        raise ValueError(f"invalid cron {label}: {source}") from error


def _parse_sqlite_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc)


def _has_missed_run(expression: CronExpression, after: datetime, through: datetime) -> bool:
    candidate = expression.next_after(after, limit_minutes=527_040)
    return candidate is not None and candidate <= through
