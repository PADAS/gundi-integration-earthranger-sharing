"""Site identity and source-system naming (pure)."""
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse


@dataclass(frozen=True)
class SourceSystem:
    value: str  # prefix for category and event-type values, e.g. "sitea"
    name: str   # prefix for display names and titles, e.g. "Sitea"


def site_host(url: str) -> str:
    """Lower-cased hostname of an ER site URL, with or without a scheme."""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("Site URL has no hostname")
    return host


def site_root(url: str) -> str:
    return f"https://{site_host(url)}"


def same_site(url_a: str, url_b: str) -> bool:
    return site_host(url_a) == site_host(url_b)


def derive_source_system(url: str, value: Optional[str] = None, name: Optional[str] = None) -> SourceSystem:
    """v1 kept these in state; v2 derives them from the source host unless a
    migrated share overrides them (compatibility mode)."""
    label = site_host(url).split(".")[0]
    return SourceSystem(value=value or label, name=name or label.capitalize())
