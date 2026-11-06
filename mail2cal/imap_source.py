"""Lee del buzón IMAP los correos nuevos que traen invitaciones (.ics / text/calendar).

El buzón se abre en modo solo lectura y los mensajes se piden con BODY.PEEK, así que
nunca se marcan como leídos ni se modifican.
"""
from __future__ import annotations

import email
import email.policy
import imaplib
import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta

log = logging.getLogger(__name__)

CALENDAR_TYPES = {"text/calendar", "application/ics", "application/x-ics"}
# Pista en BODYSTRUCTURE de que el mensaje lleva un calendario adjunto o en línea.
CALENDAR_HINT = re.compile(rb'"text"\s+"calendar"|"application"\s+"(?:x-)?ics"|\.ics\b', re.I)
FETCH_START = re.compile(rb"^\d+ \(")
UID_RE = re.compile(rb"\bUID (\d+)")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


@dataclass
class FolderBatch:
    messages: list[tuple[int, bytes]]  # (UID, mensaje RFC 822 completo), en orden de llegada
    next_state: dict                   # {"uidvalidity": int, "last_uid": int}
    scanned: int                       # cuántos mensajes nuevos se revisaron


class Mailbox:
    def __init__(self, host: str, port: int, user: str, password: str, *, backfill_days: int = 30,
                 timeout: int = 60):
        self.host, self.port, self.user, self.password = host, port, user, password
        self.backfill_days = backfill_days
        self.timeout = timeout
        self.conn: imaplib.IMAP4_SSL | None = None

    def __enter__(self) -> Mailbox:
        self.conn = imaplib.IMAP4_SSL(self.host, self.port, timeout=self.timeout)
        self.conn.login(self.user, self.password)
        return self

    def __exit__(self, *exc) -> None:
        if self.conn is not None:
            try:
                self.conn.logout()
            except Exception:
                pass

    def fetch(self, folder: str, prev: dict | None) -> FolderBatch:
        conn = self.conn
        typ, data = conn.select(_quote(folder), readonly=True)
        if typ != "OK":
            raise RuntimeError(f"No se pudo abrir la carpeta IMAP {folder!r}: {data!r}")
        uidvalidity = int(_response_value(conn, "UIDVALIDITY") or 0)
        uidnext = _response_value(conn, "UIDNEXT")
        if uidnext is None:
            uidnext = self._status_uidnext(folder)

        resumed = bool(prev and prev.get("uidvalidity") == uidvalidity)
        if resumed:
            last = int(prev.get("last_uid", 0))
            criteria = ("UID", f"{last + 1}:*")
        else:
            # Primera ejecución (o el servidor renumeró la carpeta): revisar los últimos días.
            last = 0
            since = date.today() - timedelta(days=self.backfill_days)
            criteria = ("SINCE", f"{since.day:02d}-{MONTHS[since.month - 1]}-{since.year}")

        typ, data = conn.uid("SEARCH", *criteria)
        if typ != "OK":
            raise RuntimeError(f"Falló la búsqueda IMAP en {folder!r}: {data!r}")
        # "N:*" siempre devuelve al menos el último mensaje, aunque sea anterior a N.
        uids = sorted(u for u in (int(x) for x in (data[0] or b"").split()) if u > last)

        # En la primera pasada se parte de UIDNEXT (tomado al abrir la carpeta), para que la
        # siguiente no recorra todo el buzón aunque en los últimos días no haya llegado nada.
        baseline = last if resumed else max(0, int(uidnext or 1) - 1)
        next_state = {"uidvalidity": uidvalidity, "last_uid": max([baseline, *uids])}

        messages = []
        for uid in self._calendar_candidates(uids):
            typ, data = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
            raw = next((item[1] for item in data or [] if isinstance(item, tuple)), None)
            if typ == "OK" and raw:
                messages.append((uid, raw))
        log.info("%s: %d mensajes nuevos, %d con calendario", folder, len(uids), len(messages))
        return FolderBatch(messages=messages, next_state=next_state, scanned=len(uids))

    def _calendar_candidates(self, uids: list[int]) -> list[int]:
        """Filtra por BODYSTRUCTURE para no descargar correos que no traen invitación."""
        hits: set[int] = set()
        seen: set[int] = set()
        for i in range(0, len(uids), 200):
            chunk = uids[i:i + 200]
            typ, data = self.conn.uid("FETCH", ",".join(map(str, chunk)), "(UID BODYSTRUCTURE)")
            if typ != "OK":
                continue
            for blob in group_fetch_items(data):
                match = UID_RE.search(blob)
                if not match:
                    continue
                uid = int(match.group(1))
                seen.add(uid)
                if CALENDAR_HINT.search(blob):
                    hits.add(uid)
        # Lo que no se pudo clasificar se descarga completo: mejor revisar de más que perder una invitación.
        return sorted(hits | (set(uids) - seen))

    def _status_uidnext(self, folder: str) -> int | None:
        typ, data = self.conn.status(_quote(folder), "(UIDNEXT)")
        match = re.search(rb"UIDNEXT (\d+)", data[0] or b"") if typ == "OK" and data else None
        return int(match.group(1)) if match else None


def group_fetch_items(data: list) -> list[bytes]:
    """Agrupa la respuesta de imaplib a un FETCH en un bloque de bytes por mensaje."""
    groups: list[bytes] = []
    for item in data or []:
        if item is None:
            continue
        chunk = item[0] + item[1] if isinstance(item, tuple) else item
        head = item[0] if isinstance(item, tuple) else item
        if FETCH_START.match(head) or not groups:
            groups.append(chunk)
        else:
            groups[-1] += chunk
    return groups


def extract_calendars(raw: bytes) -> list[str]:
    """Devuelve el texto de cada parte iCalendar del mensaje (incluye correos reenviados)."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    found: list[str] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = (part.get_filename() or "").lower()
        if part.get_content_type() not in CALENDAR_TYPES and not filename.endswith(".ics"):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if "BEGIN:VCALENDAR" in text.upper() and text not in found:
            found.append(text)
    return found


def _response_value(conn: imaplib.IMAP4, code: str) -> int | None:
    _, data = conn.response(code)
    if data and data[0]:
        try:
            return int(data[0])
        except (TypeError, ValueError):
            return None
    return None


def _quote(folder: str) -> str:
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'
