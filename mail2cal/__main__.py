"""Uso local:

    python -m mail2cal run [--dry-run] [--env-file .env]   # una pasada de sincronización
    python -m mail2cal parse invitacion.eml|invitacion.ics # ver qué se crearía, sin red
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .config import Config, ConfigError, load_dotenv
from .ics_convert import parse_calendar
from .imap_source import extract_calendars
from .sync import run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mail2cal", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", default=".env", help="archivo de configuración (por defecto .env)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="leer el correo y actualizar Google Calendar")
    run_p.add_argument("--dry-run", action="store_true",
                       help="solo leer el correo y mostrar qué se haría (no toca Google ni guarda estado)")
    parse_p = sub.add_parser("parse", help="mostrar cómo se convertiría un .eml o .ics")
    parse_p.add_argument("file", type=Path)
    parse_p.add_argument("--tz", help="zona horaria por defecto (si no, DEFAULT_TIMEZONE o UTC)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv(args.env_file)

    if args.command == "parse":
        import os
        tz = args.tz or os.environ.get("DEFAULT_TIMEZONE") or "UTC"
        raw = args.file.read_bytes()
        texts = [raw.decode("utf-8", "replace")] if raw.lstrip().upper().startswith(b"BEGIN:VCALENDAR") \
            else extract_calendars(raw)
        if not texts:
            print("No se encontró ninguna invitación (.ics / text/calendar) en el archivo.", file=sys.stderr)
            return 1
        for text in texts:
            for inv in parse_calendar(text, tz):
                print(json.dumps({"action": inv.action, "uid": inv.uid, "recurrence_id": inv.recurrence_id,
                                  "sequence": inv.sequence, "stamp": inv.stamp, "event": inv.body},
                                 ensure_ascii=False, indent=2))
        return 0

    try:
        cfg = Config.from_env(require_google=not args.dry_run)
    except ConfigError as exc:
        print(f"Error de configuración: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(run(cfg, dry_run=args.dry_run), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
