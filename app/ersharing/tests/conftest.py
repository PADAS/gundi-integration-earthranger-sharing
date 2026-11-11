import pytest

from app.ersharing import clients


@pytest.fixture(autouse=True)
def no_retry_pauses(monkeypatch):
    """Keep within-run retries but skip their pauses, so tests stay fast."""
    monkeypatch.setattr(clients, "RETRY_WAITS", (0, 0))
