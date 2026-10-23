# Gundi Integration: EarthRanger Sharing

Mirrors events and subject tracks from one EarthRanger site (the connection's
source) to every EarthRanger destination of the connection. It is a one-way
live mirror: edits, notes, attachments, incident links and deletes follow the source.

Design: `docs/superpowers/specs/2026-10-15-ersharing-v2-design.md`.

## Actions

| Action | Kind | Schedule |
|---|---|---|
| `auth` | Validates the source token and reports what it can read | — |
| `list_event_types`, `list_subject_groups`, `list_feature_groups` | Pick-lists for the form | — |
| `sync_events` | Copies new/changed events; once a day removes copies whose source was deleted | every 5 min |
| `sync_observations` | Shares subject track points through each destination's sharing endpoint | every 5 min |

Destinations are Gundi `earth_ranger` integrations. Their stored credentials are
read through Gundi at run time, and the runner writes to each destination directly.

## Development

```bash
pip-compile --output-file=requirements.txt requirements-base.in requirements-dev.in requirements.in
pip install -r requirements.txt
pytest
```

Code lives in `app/ersharing/`; `app/actions/handlers.py` only wires actions to it.
Handlers import `app.ersharing` inside function bodies (see the module docstring).
