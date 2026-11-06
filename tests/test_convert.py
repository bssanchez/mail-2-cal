from mail2cal.ics_convert import parse_calendar, time_key
from mail2cal.imap_source import extract_calendars, group_fetch_items

from .samples import (ALL_DAY, GOOGLE_INVITE, OUTLOOK_CANCEL, OUTLOOK_INSTANCE_MOVED, OUTLOOK_RECURRING,
                      REPLY, email_with)

TZ = "America/Bogota"


def test_outlook_recurring_request():
    [inv] = parse_calendar(OUTLOOK_RECURRING, TZ)
    body = inv.body
    assert inv.action == "upsert" and inv.sequence == 0 and inv.stamp == "20261106T150000Z"
    assert body["iCalUID"] == "040000008200E00074C5B7101A82E00800000000ABCDEF"
    assert body["summary"] == "Seguimiento semanal"
    # Nombre de zona de Windows traducido a IANA, con el instante correcto.
    assert body["start"] == {"dateTime": "2026-11-10T09:00:00-05:00", "timeZone": "America/Bogota"}
    assert body["end"]["dateTime"] == "2026-11-10T10:00:00-05:00"
    assert body["recurrence"] == [
        "RRULE:FREQ=WEEKLY;UNTIL=20261222T140000Z;INTERVAL=1;BYDAY=TU;WKST=SU",
        "EXDATE:20261117T140000Z",
    ]
    assert body["description"].startswith("Enlace de la reunión: https://teams.microsoft.com/l/meetup-join/abc")
    assert body["organizer"] == {"email": "ana@cliente.com", "displayName": "Ana Jefa"}
    by_email = {a["email"]: a for a in body["attendees"]}
    assert by_email["yo@miempresa.com"]["responseStatus"] == "needsAction"
    assert by_email["pedro@cliente.com"] == {"email": "pedro@cliente.com", "responseStatus": "accepted",
                                             "displayName": "Pedro", "optional": True}
    assert by_email["sala1@cliente.com"]["resource"] is True
    assert body["extendedProperties"]["private"]["m2c_seq"] == "0"


def test_google_invite_uses_meet_link_and_iana_zone():
    [inv] = parse_calendar(GOOGLE_INVITE, TZ)
    assert inv.body["start"] == {"dateTime": "2026-11-12T15:00:00-06:00", "timeZone": "America/Mexico_City"}
    assert "https://meet.google.com/abc-defg-hij" in inv.body["description"]
    assert "location" not in inv.body


def test_cancel():
    [inv] = parse_calendar(OUTLOOK_CANCEL, TZ)
    assert inv.action == "cancel" and inv.sequence == 2 and inv.body == {}


def test_single_instance_change():
    [inv] = parse_calendar(OUTLOOK_INSTANCE_MOVED, TZ)
    assert inv.recurrence_key == "20261124T140000Z"
    assert "recurrence" not in inv.body
    # UTC se muestra en la zona horaria del usuario.
    assert inv.body["start"] == {"dateTime": "2026-11-24T16:00:00+00:00", "timeZone": "America/Bogota"}


def test_all_day_without_end():
    [inv] = parse_calendar(ALL_DAY, TZ)
    assert inv.action == "upsert"  # sin METHOD = PUBLISH
    assert inv.body["start"] == {"date": "2026-12-08"} and inv.body["end"] == {"date": "2026-12-09"}
    assert inv.body["transparency"] == "transparent"


def test_reply_is_ignored():
    assert parse_calendar(REPLY, TZ) == []


def test_extract_from_email_dedupes_inline_and_attachment():
    assert extract_calendars(email_with(OUTLOOK_RECURRING)) == [OUTLOOK_RECURRING]


def test_extract_from_plain_email():
    from email.message import EmailMessage
    msg = EmailMessage()
    msg.set_content("hola")
    assert extract_calendars(msg.as_bytes()) == []


def test_time_key():
    assert time_key({"dateTime": "2026-11-24T09:00:00-05:00"}) == "20261124T140000Z"
    assert time_key({"dateTime": "2026-11-24T14:00:00Z"}) == "20261124T140000Z"
    assert time_key({"date": "2026-11-24"}) == "2026-11-24"


def test_group_fetch_items_with_literals():
    data = [
        b'1 (UID 10 BODYSTRUCTURE (("TEXT" "PLAIN" NIL NIL NIL "7BIT" 10 1 NIL NIL NIL)))',
        (b'2 (UID 11 BODYSTRUCTURE (("TEXT" "PLAIN" NIL NIL NIL "7BIT" 10 1)("APPLICATION" "OCTET-STREAM"'
         b' ("NAME" {10}', b'invite.ics'),
        b') NIL NIL "BASE64" 100)))',
        b'3 (UID 12 BODYSTRUCTURE ("TEXT" "CALENDAR" ("METHOD" "REQUEST") NIL NIL "7BIT" 50 3))',
    ]
    groups = group_fetch_items(data)
    assert len(groups) == 3
    assert b"invite.ics" in groups[1] and groups[1].endswith(b'"BASE64" 100)))')
