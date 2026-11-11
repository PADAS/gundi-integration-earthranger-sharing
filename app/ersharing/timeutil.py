from datetime import datetime, timezone
from typing import Union

from dateutil.parser import isoparse


def parse_dt(value: Union[str, datetime]) -> datetime:
    """An ER timestamp as an aware UTC datetime (naive values are taken as UTC)."""
    dt = value if isinstance(value, datetime) else isoparse(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
