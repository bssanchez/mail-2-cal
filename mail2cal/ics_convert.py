"""Convierte invitaciones iCalendar (iTIP, RFC 5546) en recursos de Google Calendar."""
from __future__ import annotations

import logging
import re
import zoneinfo
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import icalendar
from icalendar.timezone.windows_to_olson import WINDOWS_TO_OLSON

log = logging.getLogger(__name__)

# REQUEST/PUBLISH crean o actualizan. REPLY, COUNTER, REFRESH... son respuestas de
# otros asistentes a reuniones que organizas tú, y ADD solo trae instancias sueltas:
# ninguno de esos debe tocar tu calendario.
UPSERT_METHODS = {"REQUEST", "PUBLISH"}
CANCEL_METHODS = {"CANCEL"}

MAX_DESCRIPTION = 8000
MEETING_URL_PROPS = ("X-MICROSOFT-SKYPETEAMSMEETINGURL", "X-GOOGLE-CONFERENCE", "CONFERENCE", "URL")
MEETING_URL_RE = re.compile(
    r"https://(?:teams\.microsoft\.com|teams\.live\.com|[\w.-]*zoom\.us|meet\.google\.com"
    r"|[\w.-]*webex\.com|meet\.jit\.si|[\w.-]*whereby\.com)/[^\s<>\"')\]]+",
    re.I,
)
PARTSTAT = {"ACCEPTED": "aceptó", "DECLINED": "rechazó", "TENTATIVE": "quizás"}
MAX_LISTED_ATTENDEES = 40
# Sube este número cuando cambie cómo se arma el evento: los ya importados se reescriben.
FORMAT_VERSION = "2"


@dataclass
class Invite:
    action: str                      # "upsert" o "cancel"
    uid: str
    sequence: int
    stamp: str                       # DTSTAMP en UTC (YYYYMMDDTHHMMSSZ), desempata versiones
    summary: str
    recurrence_id: dict | None = None  # {"dateTime": ...} o {"date": ...} si es una sola instancia
    body: dict = field(default_factory=dict)  # recurso de Google Calendar (solo para upsert)

    @property
    def recurrence_key(self) -> str | None:
        return time_key(self.recurrence_id) if self.recurrence_id else None

    @property
    def key(self) -> tuple[str, str | None]:
        return (self.uid, self.recurrence_key)

    @property
    def version(self) -> tuple[int, str]:
        return (self.sequence, self.stamp)


def parse_calendar(text: str, default_tz: str) -> list[Invite]:
    """Extrae las invitaciones de un texto iCalendar. Ignora los VEVENT que no entiende."""
    invites: list[Invite] = []
    for cal in icalendar.Calendar.from_ical(text, multiple=True):
        method = str(cal.get("METHOD") or "PUBLISH").strip().upper()
        for ev in cal.walk("VEVENT"):
            try:
                invite = _to_invite(ev, method, default_tz)
            except Exception as exc:  # un evento raro no debe tumbar el resto
                log.warning("VEVENT ignorado (UID=%s): %s", ev.get("UID"), exc)
                continue
            if invite:
                invites.append(invite)
    return invites


def _to_invite(ev: icalendar.Event, method: str, default_tz: str) -> Invite | None:
    uid = str(ev.get("UID") or "").strip()
    if not uid:
        raise ValueError("el evento no tiene UID")
    status = str(ev.get("STATUS") or "").upper()

    if method in CANCEL_METHODS or status == "CANCELLED":
        action = "cancel"
    elif method in UPSERT_METHODS:
        action = "upsert"
    else:
        log.debug("METHOD %s ignorado (UID=%s)", method, uid)
        return None

    sequence = int(str(ev.get("SEQUENCE") or 0))
    stamp = _utc_stamp(ev, "DTSTAMP") or _utc_stamp(ev, "LAST-MODIFIED") or ""
    rid = None
    if "RECURRENCE-ID" in ev:
        rid = _gtime(ev.decoded("RECURRENCE-ID"), ev["RECURRENCE-ID"], default_tz)

    invite = Invite(
        action=action,
        uid=uid,
        sequence=sequence,
        stamp=stamp,
        summary=str(ev.get("SUMMARY") or "").strip(),
        recurrence_id=rid,
    )
    if action == "upsert":
        invite.body = _event_body(ev, invite, status, default_tz)
    return invite


def _event_body(ev: icalendar.Event, invite: Invite, status: str, default_tz: str) -> dict:
    if "DTSTART" not in ev:
        raise ValueError("el evento no tiene DTSTART")
    start = ev.decoded("DTSTART")
    all_day = not isinstance(start, datetime)

    if "DTEND" in ev:
        end = ev.decoded("DTEND")
        end_prop = ev["DTEND"]
    elif "DURATION" in ev:
        end = start + ev.decoded("DURATION")
        end_prop = ev["DTSTART"]
    else:
        end = start + timedelta(days=1) if all_day else start
        end_prop = ev["DTSTART"]

    if all_day:
        if isinstance(end, datetime):
            end = end.date()
        if end <= start:
            end = start + timedelta(days=1)
    elif not isinstance(end, datetime) or _aware(end, default_tz) < _aware(start, default_tz):
        end = start

    location = str(ev.get("LOCATION") or "").strip()
    original = str(ev.get("DESCRIPTION") or "").strip()
    meeting_url = _meeting_url(ev, original, location)
    # Organizador e invitados van como texto: si se pasaran a Google, el evento quedaría con
    # tu Gmail como organizador y Google podría escribirles al editarlo o borrarlo.
    header = [f"Enlace de la reunión: {meeting_url}"] if meeting_url else []
    header += _people_lines(ev)
    description = "\n".join(header) + ("\n\n" + original if original else "")
    description = description.strip()
    if len(description) > MAX_DESCRIPTION:
        description = description[: MAX_DESCRIPTION - 20].rstrip() + "\n\n[…recortado]"

    body: dict = {
        "iCalUID": invite.uid,
        "summary": invite.summary or "(Sin título)",
        "start": _gtime(start, ev["DTSTART"], default_tz),
        "end": _gtime(end, end_prop, default_tz),
        "status": "tentative" if status == "TENTATIVE" else "confirmed",
        "transparency": "transparent" if str(ev.get("TRANSP") or "").upper() == "TRANSPARENT" else "opaque",
        "reminders": {"useDefault": True},
        "extendedProperties": {
            "private": {"mail2cal": "1", "m2c_seq": str(invite.sequence), "m2c_stamp": invite.stamp,
                        "m2c_fmt": FORMAT_VERSION}
        },
    }
    if location:
        body["location"] = location
    if description:
        body["description"] = description
    if str(ev.get("CLASS") or "").upper() in ("PRIVATE", "CONFIDENTIAL"):
        body["visibility"] = "private"

    if invite.recurrence_id is None:
        recurrence = _recurrence(ev, default_tz)
        if recurrence:
            body["recurrence"] = recurrence
    return body


def _people_lines(ev: icalendar.Event) -> list[str]:
    lines = []
    organizer = ev.get("ORGANIZER")
    organizer_email = _mailto(organizer).lower() if organizer is not None else ""
    if "@" in organizer_email:
        lines.append(f"Organiza: {_person(organizer)}")
    people = []
    for prop in _as_list(ev.get("ATTENDEE")):
        email = _mailto(prop)
        if "@" not in email or email.lower() == organizer_email:
            continue
        notes = []
        if str(prop.params.get("CUTYPE") or "").upper() in ("ROOM", "RESOURCE"):
            notes.append("sala")
        elif str(prop.params.get("ROLE") or "").upper() == "OPT-PARTICIPANT":
            notes.append("opcional")
        status = PARTSTAT.get(str(prop.params.get("PARTSTAT") or "").upper())
        if status:
            notes.append(status)
        people.append(_person(prop) + (f" ({', '.join(notes)})" if notes else ""))
    if people:
        listed = people[:MAX_LISTED_ATTENDEES]
        extra = len(people) - len(listed)
        lines.append("Invitados: " + "; ".join(listed) + (f"; y {extra} más" if extra > 0 else ""))
    return lines


def _person(prop) -> str:
    email = _mailto(prop)
    name = str(prop.params.get("CN") or "").strip().strip('"')
    return f"{name} <{email}>" if name and name.lower() != email.lower() else email


def _recurrence(ev: icalendar.Event, default_tz: str) -> list[str]:
    lines = ["RRULE:" + r.to_ical().decode() for r in _as_list(ev.get("RRULE"))]
    for name in ("EXDATE", "RDATE"):
        for prop in _as_list(ev.get(name)):
            for item in getattr(prop, "dts", []):
                value = item.dt
                if isinstance(value, tuple):  # RDATE de tipo PERIOD: Google no lo soporta
                    continue
                if isinstance(value, datetime):
                    utc = _aware(value, default_tz).astimezone(timezone.utc)
                    lines.append(f"{name}:{utc:%Y%m%dT%H%M%SZ}")
                else:
                    lines.append(f"{name};VALUE=DATE:{value:%Y%m%d}")
    return lines


def _meeting_url(ev: icalendar.Event, description: str, location: str) -> str | None:
    for name in MEETING_URL_PROPS:
        for value in _as_list(ev.get(name)):
            url = str(value).strip()
            if url.lower().startswith("https://"):
                return url
    for text in (location, description):
        match = MEETING_URL_RE.search(text)
        if match:
            return match.group(0)
    return None


def _gtime(value, prop, default_tz: str) -> dict:
    """Fecha/hora de iCalendar -> {"dateTime", "timeZone"} o {"date"} de Google."""
    if not isinstance(value, datetime):
        return {"date": value.isoformat()}
    tzid = prop.params.get("TZID") if prop is not None and hasattr(prop, "params") else None
    tzname = iana_name(tzid, value.tzinfo)
    if value.tzinfo is None:  # hora "flotante": se asume la zona del evento o la tuya
        value = value.replace(tzinfo=zoneinfo.ZoneInfo(tzname or default_tz))
    return {"dateTime": value.isoformat(timespec="seconds"), "timeZone": tzname or default_tz}


def iana_name(tzid, tzinfo) -> str | None:
    """Nombre IANA para mostrar el evento. Outlook usa nombres de Windows ("SA Pacific Standard Time")."""
    for candidate in (str(tzid).strip('"') if tzid else None, getattr(tzinfo, "key", None)):
        if not candidate:
            continue
        if candidate.upper() in ("UTC", "GMT", "Z", "ETC/UTC", "ETC/GMT"):
            return None  # UTC se muestra en tu zona horaria
        if candidate in WINDOWS_TO_OLSON:
            return WINDOWS_TO_OLSON[candidate]
        try:
            zoneinfo.ZoneInfo(candidate)
            return candidate
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            continue
    return None


def time_key(gtime: dict) -> str:
    """Clave estable para comparar instantes de Google ({"dateTime"} o {"date"})."""
    if "date" in gtime:
        return gtime["date"]
    dt = datetime.fromisoformat(gtime["dateTime"].replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=zoneinfo.ZoneInfo(gtime.get("timeZone") or "UTC"))
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _aware(value: datetime, default_tz: str) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=zoneinfo.ZoneInfo(default_tz))


def _utc_stamp(ev: icalendar.Event, name: str) -> str | None:
    if name not in ev:
        return None
    value = ev.decoded(name)
    if not isinstance(value, datetime):
        return None
    return _aware(value, "UTC").astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _mailto(value) -> str:
    text = str(value).strip()
    if text.lower().startswith("mailto:"):
        text = text[len("mailto:"):]
    return text.strip()


def _as_list(value) -> list:
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


__all__ = ["Invite", "parse_calendar", "time_key", "iana_name"]
