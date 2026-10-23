"""What admins see in the activity log for each run and destination (spec §9)."""
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from gundi_core.events import LogLevel

from app.services.activity_logger import log_action_activity
from .state import Marker

STALL_AFTER = timedelta(hours=6)
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


def is_stalled(marker: Optional[Marker], now: datetime) -> bool:
    return marker is not None and now - marker.advanced_at > STALL_AFTER
