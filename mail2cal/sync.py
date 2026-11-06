"""Orquesta una pasada: correo nuevo -> invitaciones -> Google Calendar."""
from __future__ import annotations

import logging
import re
from collections import Counter

from .config import Config
from .gcal import GoogleApiError, GoogleCalendar
from .ics_convert import FORMAT_VERSION, Invite, parse_calendar
from .imap_source import Mailbox, extract_calendars
from .state import FileState, SsmState

log = logging.getLogger(__name__)

# Campos que se copian a una instancia concreta de una serie (RECURRENCE-ID).
INSTANCE_FIELDS = ("summary", "description", "location", "start", "end", "status", "transparency",
                   "extendedProperties", "colorId", "reminders")


def run(cfg: Config, *, dry_run: bool = False, mailbox=None, calendar=None, store=None) -> dict:
    store = store or (SsmState(cfg.state_ssm_param) if cfg.state_backend == "ssm" else FileState(cfg.state_path))
    state = store.load()
    folders_state = state.setdefault("folders", {})

    invites: list[Invite] = []
    new_folder_states: dict[str, dict] = {}
    stats: Counter = Counter()

    mailbox = mailbox or Mailbox(cfg.imap_host, cfg.imap_port, cfg.imap_user, cfg.imap_password,
                                 backfill_days=cfg.backfill_days)
    with mailbox:
        for folder in cfg.imap_folders:
            batch = mailbox.fetch(folder, folders_state.get(folder))
            new_folder_states[folder] = batch.next_state
            stats["mensajes_revisados"] += batch.scanned
            for uid, raw in batch.messages:
                for text in extract_calendars(raw):
                    try:
                        parsed = parse_calendar(text, cfg.default_timezone)
                    except Exception as exc:
                        log.warning("%s UID %s: calendario ilegible (%s)", folder, uid, exc)
                        continue
                    for inv in parsed:
                        customize(inv, cfg)
                    invites.extend(parsed)

    pending = latest_per_event(invites)
    stats["invitaciones"] = len(pending)

    if dry_run:
        for inv in pending:
            log.info("[simulación] %s %s %r (UID=%s%s)", inv.action, _when(inv), inv.summary, inv.uid,
                     f", instancia {inv.recurrence_key}" if inv.recurrence_key else "")
        return dict(stats)

    calendar = calendar or GoogleCalendar(cfg.google_client_id, cfg.google_client_secret,
                                          cfg.google_refresh_token, cfg.google_calendar_id)
    for inv in pending:
        try:
            result = apply_invite(inv, calendar)
        except GoogleApiError as exc:
            if not exc.permanent:
                # Fallo temporal o de credenciales: no se guarda el estado y la próxima pasada
                # reintenta todo el lote (aplicar dos veces la misma invitación no duplica nada).
                raise
            log.error("Invitación %r (UID=%s) descartada: %s", inv.summary, inv.uid, exc)
            result = "error"
        stats[result] += 1
        if result != "sin_cambios":
            log.info("%s: %r %s", result, inv.summary, _when(inv))

    folders_state.update(new_folder_states)
    store.save(state)
    log.info("Resumen: %s", dict(stats))
    return dict(stats)


def customize(inv: Invite, cfg: Config) -> None:
    """Aplica tus preferencias (color, avisos) y las anota en la huella de formato del evento."""
    if inv.action != "upsert":
        return
    if cfg.event_color_id:
        inv.body["colorId"] = cfg.event_color_id
    # Solo avisos en la app (popup); nunca por correo.
    inv.body["reminders"] = {
        "useDefault": False,
        "overrides": [{"method": "popup", "minutes": m} for m in cfg.reminder_minutes],
    }
    reminders = "-".join(map(str, cfg.reminder_minutes))
    inv.body["extendedProperties"]["private"]["m2c_fmt"] = \
        f"{FORMAT_VERSION};c={cfg.event_color_id};r={reminders}"


def latest_per_event(invites: list[Invite]) -> list[Invite]:
    """Se queda con la versión más reciente de cada evento/instancia del lote.

    Así, si en el lote hay una invitación y luego su cancelación, solo se aplica la cancelación.
    Las series van primero para que existan antes de modificar sus instancias.
    """
    latest: dict[tuple, Invite] = {}
    for inv in invites:
        current = latest.get(inv.key)
        if current is None or inv.version > current.version:
            latest[inv.key] = inv
    return sorted(latest.values(), key=lambda i: i.recurrence_id is not None)


def apply_invite(inv: Invite, cal) -> str:
    if inv.recurrence_id is not None:
        result = _apply_instance(inv, cal)
        if result:
            return result
        # No tenemos la serie (p. ej. solo te invitaron a una sesión): se crea como evento suelto.
        return _apply_event(inv, cal, uid=_standalone_uid(inv))
    return _apply_event(inv, cal, uid=inv.uid)


def _apply_event(inv: Invite, cal, uid: str) -> str:
    existing = cal.find_by_uid(uid)
    if inv.action == "cancel":
        if existing and existing.get("status") != "cancelled":
            cal.delete_event(existing["id"])
            return "eliminado"
        return "sin_cambios"

    body = {**inv.body, "iCalUID": uid}
    if existing is None:
        cal.import_event(body)
        return "creado"
    if existing.get("status") != "cancelled" and is_current(existing, inv):
        return "sin_cambios"
    # PUT reemplaza el evento completo: también quita invitados que hubieran quedado de antes.
    cal.update_event(existing["id"], body)
    return "actualizado"


def _apply_instance(inv: Invite, cal) -> str | None:
    parent = cal.find_by_uid(inv.uid)
    if not parent or parent.get("status") == "cancelled" or not parent.get("recurrence"):
        return None
    instance = cal.find_instance(parent["id"], inv.recurrence_id)
    if not instance:
        return None
    if inv.action == "cancel":
        if instance.get("status") == "cancelled":
            return "sin_cambios"
        cal.patch_event(instance["id"], {"status": "cancelled"})
        return "eliminado"
    if instance.get("status") != "cancelled" and is_current(instance, inv):
        return "sin_cambios"
    cal.patch_event(instance["id"], {k: inv.body[k] for k in INSTANCE_FIELDS if k in inv.body})
    return "actualizado"


def is_current(event: dict, inv: Invite) -> bool:
    """True si el evento ya refleja esta versión de la invitación con el formato y preferencias actuales."""
    props = event.get("extendedProperties", {}).get("private", {})
    wanted = inv.body.get("extendedProperties", {}).get("private", {}).get("m2c_fmt", FORMAT_VERSION)
    return props.get("m2c_fmt") == wanted and stored_version(event) >= inv.version


def stored_version(event: dict) -> tuple[int, str]:
    props = event.get("extendedProperties", {}).get("private", {})
    if "m2c_seq" not in props:
        return (-1, "")
    try:
        return (int(props["m2c_seq"]), props.get("m2c_stamp", ""))
    except ValueError:
        return (-1, "")


def _standalone_uid(inv: Invite) -> str:
    return f"{inv.uid}-{re.sub(r'[^0-9A-Za-z]', '', inv.recurrence_key or '')}"


def _when(inv: Invite) -> str:
    start = inv.body.get("start") or inv.recurrence_id or {}
    return start.get("dateTime") or start.get("date") or ""
