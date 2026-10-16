"""Read-only inventory of v1 EarthRanger Sharing configs. Prints aggregates only (no tokens, no endpoints).

Run inside a portal pod:
    python3 manage.py shell < ersharing_v1_inventory.py
"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from integrations.models.v1 import InboundIntegrationConfiguration

EVENT_KEYS = {"dest_endpoint", "dest_token", "source_system_value"}
SUBJECT_KEYS = {"provider_key", "look_back_window_hours", "subject_group", "subject_additional", "src_user"}
EVENT_BOOLS = ["prepend_system_to_categories", "prepend_system_to_event_types",
               "prepend_system_to_event_titles", "delete_unmatched_events", "update_schema"]
NOW = datetime.now(timezone.utc)


def host_label(url):
    host = urlparse(url or "").hostname or ""
    return host.split(".")[0].lower()


def stale(ts):
    if not ts:
        return "never"
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return "unparseable"
    age = NOW - dt
    return "<1h" if age < timedelta(hours=1) else "<1d" if age < timedelta(days=1) else "<7d" if age < timedelta(days=7) else ">=7d"


def tri(state, key):
    return "missing" if key not in state else repr(state[key])


events, subjects = [], []
for cfg in InboundIntegrationConfiguration.objects.select_related("type").all():
    state = cfg.state if isinstance(cfg.state, dict) else {}
    keys = set(state)
    if keys & EVENT_KEYS:
        events.append((cfg, state))
    elif keys & SUBJECT_KEYS or "er2er" in (cfg.type.slug or "").lower():
        subjects.append((cfg, state))


def section(title):
    print(f"\n=== {title} ===")


def show(counter_title, counter):
    print(f"  {counter_title}: " + ", ".join(f"{k}={v}" for k, v in counter.most_common()))


for label, rows in (("EVENTS", events), ("SUBJECTS", subjects)):
    section(f"{label}: {len(rows)} configs")
    show("type slug", Counter(c.type.slug for c, _ in rows))
    show("enabled", Counter(c.enabled for c, _ in rows))
    show("last_successful_run age (enabled only)", Counter(stale(s.get("last_successful_run")) for c, s in rows if c.enabled))
    show("has error in state (enabled only)", Counter(bool(s.get("error")) for c, s in rows if c.enabled))
    unknown = Counter(k for _, s in rows for k in s)
    show("all state keys seen", unknown)

section("EVENTS: settings (enabled only)")
enabled_events = [(c, s) for c, s in events if c.enabled]
for key in EVENT_BOOLS:
    show(key, Counter(tri(s, key) for _, s in enabled_events))
show("days_to_sync", Counter(tri(s, "days_to_sync") for _, s in enabled_events))
show("within_featuregroups used", Counter(bool(s.get("within_featuregroups")) for _, s in enabled_events))
show("source_system_value == first label of source host",
     Counter(str(s.get("source_system_value", "")).lower() == host_label(c.endpoint) for c, s in enabled_events))
show("source_system_name present", Counter(bool(s.get("source_system_name")) for _, s in enabled_events))
show("source == destination host", Counter(host_label(c.endpoint) == host_label(s.get("dest_endpoint")) for c, s in enabled_events))
dests_per_source = defaultdict(set)
for c, s in enabled_events:
    dests_per_source[host_label(c.endpoint)].add(host_label(s.get("dest_endpoint")))
show("distinct destinations per source site", Counter(len(v) for v in dests_per_source.values()))
show("would be SIMPLE mode (all bools default True, no explicit False)",
     Counter(all(s.get(k, True) is True for k in EVENT_BOOLS) for _, s in enabled_events))

section("SUBJECTS: settings (enabled only)")
enabled_subjects = [(c, s) for c, s in subjects if c.enabled]
for key in ("provider_key", "look_back_window_hours", "schedule_interval_minutes", "batch_size"):
    show(key, Counter(tri(s, key) for _, s in enabled_subjects))
for key in ("subject_group", "subject_additional", "feature_groups", "src_user"):
    show(f"{key} set", Counter(bool(s.get(key)) for _, s in enabled_subjects))
show("destinations per config", Counter(len(list(c.destinations)) for c, _ in enabled_subjects))
show("destination types", Counter(d.type.slug for c, _ in enabled_subjects for d in c.destinations))
show("subject_group == source host label",
     Counter(str(s.get("subject_group") or "").lower() == host_label(c.endpoint) for c, s in enabled_subjects if s.get("subject_group")))
