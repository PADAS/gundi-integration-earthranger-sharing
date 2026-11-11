"""Incremental notes, attachments and incident links for one event copy (spec §7.4–7.5)."""
import hashlib
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Tuple

from gundi_core.events import LogLevel

from app.services.errors import IntegrationError
from .clients import halts_site
from .events import incident_ids
from .reporting import DestinationReport
from .state import EventRecord, NoteRecord
from .timeutil import parse_dt

MAX_FILE_BYTES = 20 * 1024 * 1024
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

TOO_BIG = "Some attachments weren't copied because they're bigger than 20 MB."


def note_digest(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


@dataclass
class NotesPlan:
    add: List[dict] = field(default_factory=list)
    update: List[Tuple[dict, str]] = field(default_factory=list)  # (source note, dest note id)
    delete: List[Tuple[str, str]] = field(default_factory=list)   # (source note id, dest note id)


@dataclass
class FilesPlan:
    add: List[dict] = field(default_factory=list)
    delete: List[Tuple[str, str]] = field(default_factory=list)   # (source file id, dest file id or "")


def oldest_first(items: List[dict]) -> List[dict]:
    """New notes and files are added oldest first, as v1 did, so the copy lists them in
    the source's order. Sorted by created_at; without it, v1's reversal of the source list."""
    if all(item.get("created_at") for item in items):
        return sorted(items, key=lambda item: parse_dt(item["created_at"]))
    return list(reversed(items))


def plan_notes(src_notes, recorded: Dict[str, NoteRecord]) -> NotesPlan:
    plan = NotesPlan()
    current = {str(n["id"]): n for n in src_notes or [] if n.get("text")}
    for src_id, note in current.items():
        rec = recorded.get(src_id)
        if rec is None:
            plan.add.append(note)
        elif rec.digest != note_digest(note["text"]):
            plan.update.append((note, rec.dest_id))
    plan.add = oldest_first(plan.add)
    plan.delete = [(src_id, rec.dest_id) for src_id, rec in recorded.items() if src_id not in current]
    return plan


def plan_files(src_files, recorded: Dict[str, str]) -> FilesPlan:
    current = {str(f["id"]): f for f in src_files or []}
    return FilesPlan(
        add=oldest_first([f for src_id, f in current.items() if src_id not in recorded]),
        delete=[(src_id, dest_id) for src_id, dest_id in recorded.items() if src_id not in current],
    )


def adopt_record(src_event: dict, dest_event: dict) -> EventRecord:
    """Build a record for a copy that exists but was never recorded (made by v1),
    matching notes by text and files by filename so nothing is copied twice."""
    record = EventRecord(dest_event_id=str(dest_event["id"]), updated_at=EPOCH)
    unused_notes = list(dest_event.get("notes") or [])
    for note in src_event.get("notes") or []:
        match = next((d for d in unused_notes if d.get("text") == note.get("text")), None)
        if match:
            unused_notes.remove(match)
            record.notes[str(note["id"])] = NoteRecord(dest_id=str(match["id"]), digest=note_digest(note.get("text")))
    unused_files = list(dest_event.get("files") or [])
    for file in src_event.get("files") or []:
        match = next((d for d in unused_files if d.get("filename") == file.get("filename")), None)
        if match:
            unused_files.remove(match)
            record.files[str(file["id"])] = str(match["id"])
    record.incidents = incident_ids(dest_event)
    return record


async def _ignore_404(awaitable):
    try:
        return await awaitable
    except IntegrationError as e:
        if e.status_code != 404:
            raise


async def apply_notes(dest, dest_event_id: str, plan: NotesPlan, record: EventRecord):
    for note in plan.add:
        created = await dest.post_note(dest_event_id, note["text"])
        record.notes[str(note["id"])] = NoteRecord(dest_id=str(created["id"]), digest=note_digest(note["text"]))
    for note, dest_note_id in plan.update:
        try:
            await dest.patch_note(dest_event_id, dest_note_id, note["text"])
            new_id = dest_note_id
        except IntegrationError as e:
            if halts_site(e) or e.status_code not in (404, 405):
                raise
            # Note edits unsupported or note gone: replace it.
            await _ignore_404(dest.delete_note(dest_event_id, dest_note_id))
            new_id = str((await dest.post_note(dest_event_id, note["text"]))["id"])
        record.notes[str(note["id"])] = NoteRecord(dest_id=new_id, digest=note_digest(note["text"]))
    for src_id, dest_note_id in plan.delete:
        await _ignore_404(dest.delete_note(dest_event_id, dest_note_id))
        record.notes.pop(src_id, None)


async def apply_files(source, dest, dest_event_id: str, plan: FilesPlan, record: EventRecord,
                      report: DestinationReport):
    for file in plan.add:
        filename = file.get("filename") or "attachment"
        async with AsyncExitStack() as stack:
            try:
                path = await stack.enter_async_context(source.download_file(file["url"], filename, MAX_FILE_BYTES))
            except IntegrationError as e:
                if halts_site(e):
                    raise
                report.error("An attachment wasn't copied because the source site wouldn't send it",
                             item=f"{filename}: {e.message}", level=LogLevel.WARNING)
                continue
            if path is None:
                report.error(TOO_BIG, item=f"{filename} wasn't copied because it's bigger than 20 MB.",
                             level=LogLevel.WARNING)
                record.files[str(file["id"])] = ""  # recorded so it isn't downloaded again
                continue
            created = await dest.post_file(dest_event_id, path, file.get("comment") or "")
        record.files[str(file["id"])] = str(created.get("id", ""))
    for src_id, dest_file_id in plan.delete:
        if dest_file_id:
            await _ignore_404(dest.delete_file(dest_event_id, dest_file_id))
        record.files.pop(src_id, None)


async def apply_incidents(dest, dest_event_id: str, wanted_ids: List[str], record: EventRecord,
                          report: DestinationReport):
    for incident_id in [i for i in wanted_ids if i not in record.incidents]:
        try:
            await dest.add_to_incident(dest_event_id, incident_id)
            record.incidents.append(incident_id)
        except IntegrationError as e:
            if halts_site(e):
                raise
            report.error("Couldn't link a copied event to its incident (the incident may not be shared)",
                         item=incident_id, level=LogLevel.WARNING)
    for incident_id in [i for i in record.incidents if i not in wanted_ids]:
        try:
            await dest.remove_from_incident(dest_event_id, incident_id)
        except IntegrationError as e:
            if halts_site(e):
                raise
            if e.status_code != 404:
                report.error("Couldn't unlink a copied event from an incident", item=incident_id,
                             level=LogLevel.WARNING)
                continue
        record.incidents.remove(incident_id)
