"""What admins see in the activity log for each run and destination (spec §9)."""
import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from gundi_core.events import LogLevel

from app.services.activity_logger import log_action_activity
from .state import Marker

STALL_AFTER = timedelta(hours=6)
# How often the same "destination skipped" line is repeated in the activity log.
SKIP_LOG_EVERY = timedelta(hours=6)
MAX_EXAMPLES = 5


@dataclass
class ErrorGroup:
    title: str
    level: LogLevel
    count: int = 0
    examples: List[str] = field(default_factory=list)


@dataclass
class DestinationReport:
    destination: str
    kind: str  # "Events" or "Tracks"
    created: int = 0
    updated: int = 0
    deleted: int = 0
    skipped: int = 0
    failed: int = 0
    marker: Optional[datetime] = None
    timed_out: bool = False
    errors: Dict[str, ErrorGroup] = field(default_factory=dict)

    def error(self, title: str, item: Optional[str] = None, level: LogLevel = LogLevel.ERROR):
        group = self.errors.setdefault(title, ErrorGroup(title=title, level=level))
        group.count += 1
        if item is not None and len(group.examples) < MAX_EXAMPLES:
            group.examples.append(item)

    def summary_title(self) -> str:
        if self.kind == "Tracks":
            counts = f"{self.created} points shared, {self.failed} failed"
        else:
            counts = (f"{self.created} created, {self.updated} updated, {self.deleted} deleted, "
                      f"{self.skipped} skipped, {self.failed} failed")
        title = f"{self.kind} → {self.destination}: {counts}"
        if self.marker:
            title += f"; synced up to {self.marker:%Y-%m-%d %H:%M} UTC"
        if self.timed_out:
            title += "; continuing in a follow-up run"
        return title

    def as_dict(self) -> dict:
        data = asdict(self)
        data["marker"] = self.marker.isoformat() if self.marker else None
        data["errors"] = {k: {"count": g.count, "examples": g.examples} for k, g in self.errors.items()}
        return data


async def publish_report(integration_id: str, action_id: str, report: DestinationReport):
    level = LogLevel.WARNING if report.failed or report.errors else LogLevel.INFO
    await log_action_activity(integration_id=integration_id, action_id=action_id,
                              title=report.summary_title(), level=level, data=report.as_dict())
    for group in report.errors.values():
        await log_action_activity(integration_id=integration_id, action_id=action_id,
                                  title=f"{report.kind} → {report.destination}: {group.title}",
                                  level=group.level, data={"count": group.count, "examples": group.examples})


async def log_skipped_destinations(integration_id: str, action_id: str, state, skipped) -> None:
    """Say why each destination was skipped, at most once per SKIP_LOG_EVERY for the
    same destination and reason, so a share that runs every few minutes doesn't
    repeat the same line all day. A new reason is logged straight away."""
    for item in skipped:
        reason_key = hashlib.sha1(item.reason.encode()).hexdigest()[:12]
        if not await state.once_per(action_id, f"skipped:{item.id}:{reason_key}",
                                    int(SKIP_LOG_EVERY.total_seconds())):
            continue
        await log_action_activity(integration_id=integration_id, action_id=action_id,
                                  title=f"Destination {item.name} skipped: {item.reason}", level=LogLevel.WARNING)


def is_stalled(marker: Optional[Marker], now: datetime) -> bool:
    return marker is not None and now - marker.advanced_at > STALL_AFTER
