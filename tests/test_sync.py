import copy
import itertools

import pytest

from mail2cal.config import Config
from mail2cal.gcal import GoogleApiError
from mail2cal.ics_convert import time_key
from mail2cal.imap_source import FolderBatch
from mail2cal.sync import run

from .samples import GOOGLE_INVITE, OUTLOOK_CANCEL, OUTLOOK_INSTANCE_MOVED, OUTLOOK_RECURRING, \
    OUTLOOK_RECURRING_UPDATED, email_with

CFG = Config(imap_user="yo@miempresa.com", imap_password="x", default_timezone="America/Bogota")


class FakeCalendar:
    """Imita lo necesario de Google Calendar, incluidas las instancias de series."""

    def __init__(self):
        self.events: dict[str, dict] = {}
        self.ids = (f"ev{i}" for i in itertools.count(1))
        self.calls: list[str] = []
        self.fail_with: GoogleApiError | None = None

    def _check(self, name):
        self.calls.append(name)
        if self.fail_with:
            raise self.fail_with

    def find_by_uid(self, uid):
        self._check("find")
        matches = [e for e in self.events.values() if e["iCalUID"] == uid and not e.get("recurringEventId")]
        return copy.deepcopy(matches[0]) if matches else None

    def find_instance(self, event_id, original_start):
        self._check("instance")
        parent = self.events[event_id]
        target = time_key(original_start)
        for e in self.events.values():
            if e.get("recurringEventId") == event_id and time_key(e["originalStartTime"]) == target:
                return copy.deepcopy(e)
        # Instancia "virtual" aún no materializada (como hace Google).
        inst = {"id": f"{event_id}_{target}", "iCalUID": parent["iCalUID"], "recurringEventId": event_id,
                "originalStartTime": original_start, "status": "confirmed",
                "extendedProperties": copy.deepcopy(parent.get("extendedProperties", {}))}
        self.events[inst["id"]] = inst
        return copy.deepcopy(inst)

    def import_event(self, body):
        self._check("import")
        event = {**copy.deepcopy(body), "id": next(self.ids)}
        self.events[event["id"]] = event
        return event

    def update_event(self, event_id, body):
        self._check("update")
        assert "organizer" not in body  # en Google es de solo lectura fuera del import
        organizer = self.events[event_id].get("organizer")
        self.events[event_id] = {**copy.deepcopy(body), "id": event_id, "organizer": organizer}
        return self.events[event_id]

    def patch_event(self, event_id, body):
        self._check("patch")
        self.events[event_id].update(copy.deepcopy(body))
        return self.events[event_id]

    def delete_event(self, event_id):
        self._check("delete")
        self.events[event_id]["status"] = "cancelled"

    def active(self):
        return [e for e in self.events.values() if e.get("status") != "cancelled"]


class FakeMailbox:
    def __init__(self, *messages: bytes):
        self.messages = list(messages)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def fetch(self, folder, prev):
        last = (prev or {}).get("last_uid", 0)
        new = [(uid, raw) for uid, raw in enumerate(self.messages, start=1) if uid > last]
        return FolderBatch(messages=new, next_state={"uidvalidity": 1, "last_uid": len(self.messages)},
                           scanned=len(new))


class MemoryStore:
    def __init__(self):
        self.state = {}
        self.saves = 0

    def load(self):
        return copy.deepcopy(self.state)

    def save(self, state):
        self.state = copy.deepcopy(state)
        self.saves += 1


def sync(mailbox, cal, store):
    return run(CFG, mailbox=mailbox, calendar=cal, store=store)


def test_create_then_rerun_is_idempotent():
    cal, store = FakeCalendar(), MemoryStore()
    box = FakeMailbox(email_with(OUTLOOK_RECURRING), email_with(GOOGLE_INVITE, also_attachment=False))
    assert sync(box, cal, store)["creado"] == 2
    assert store.state["folders"]["INBOX"] == {"uidvalidity": 1, "last_uid": 2}

    # Aunque se pierda el estado y se relea todo, no se duplica nada.
    stats = sync(box, cal, MemoryStore())
    assert stats.get("creado", 0) == 0 and stats["sin_cambios"] == 2
    assert len(cal.active()) == 2


def test_update_with_higher_sequence():
    cal, store = FakeCalendar(), MemoryStore()
    box = FakeMailbox(email_with(OUTLOOK_RECURRING))
    sync(box, cal, store)
    box.messages.append(email_with(OUTLOOK_RECURRING_UPDATED))
    assert sync(box, cal, store) == {"mensajes_revisados": 1, "invitaciones": 1, "actualizado": 1}
    [event] = cal.active()
    assert event["summary"] == "Seguimiento semanal (nuevo horario)"
    assert event["organizer"]["email"] == "ana@cliente.com"  # se conserva del import


def test_cancel_removes_event():
    cal, store = FakeCalendar(), MemoryStore()
    box = FakeMailbox(email_with(OUTLOOK_RECURRING))
    sync(box, cal, store)
    box.messages.append(email_with(OUTLOOK_CANCEL, also_attachment=False))
    assert sync(box, cal, store)["eliminado"] == 1
    assert cal.active() == []


def test_request_and_cancel_in_same_batch_only_cancels():
    cal, store = FakeCalendar(), MemoryStore()
    box = FakeMailbox(email_with(OUTLOOK_RECURRING), email_with(OUTLOOK_CANCEL))
    stats = sync(box, cal, store)
    assert stats["sin_cambios"] == 1 and "import" not in cal.calls


def test_instance_change_patches_occurrence():
    cal, store = FakeCalendar(), MemoryStore()
    box = FakeMailbox(email_with(OUTLOOK_INSTANCE_MOVED), email_with(OUTLOOK_RECURRING))
    stats = sync(box, cal, store)  # la serie se aplica antes que la instancia, aunque llegue después
    assert stats["creado"] == 1 and stats["actualizado"] == 1
    inst = next(e for e in cal.events.values() if e.get("recurringEventId"))
    assert inst["start"]["dateTime"] == "2026-11-24T16:00:00+00:00"


def test_instance_without_series_becomes_standalone_event():
    cal, store = FakeCalendar(), MemoryStore()
    assert sync(FakeMailbox(email_with(OUTLOOK_INSTANCE_MOVED)), cal, store)["creado"] == 1
    [event] = cal.active()
    assert event["iCalUID"].endswith("-20261124T140000Z")


def test_transient_error_keeps_state_for_retry():
    cal, store = FakeCalendar(), MemoryStore()
    cal.fail_with = GoogleApiError(503, "backend error")
    with pytest.raises(GoogleApiError):
        sync(FakeMailbox(email_with(OUTLOOK_RECURRING)), cal, store)
    assert store.saves == 0


def test_permanent_error_skips_invite_and_advances():
    cal, store = FakeCalendar(), MemoryStore()
    cal.fail_with = GoogleApiError(400, "invalid")
    stats = sync(FakeMailbox(email_with(OUTLOOK_RECURRING)), cal, store)
    assert stats["error"] == 1 and store.state["folders"]["INBOX"]["last_uid"] == 1
