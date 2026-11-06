"""Configuración leída de variables de entorno (o de un archivo .env en local)."""
from __future__ import annotations

import json
import os
import zoneinfo
from dataclasses import dataclass
from pathlib import Path

REQUIRED_IMAP = ("IMAP_USER", "IMAP_PASSWORD")
REQUIRED_GOOGLE = ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    imap_user: str
    imap_password: str
    imap_host: str = "imap.hostinger.com"
    imap_port: int = 993
    imap_folders: tuple[str, ...] = ("INBOX",)
    google_client_id: str = ""
    google_client_secret: str = ""
    google_refresh_token: str = ""
    google_calendar_id: str = "primary"
    default_timezone: str = "UTC"
    event_color_id: str = ""  # colorId de Google Calendar ("1".."11"); vacío = color del calendario
    backfill_days: int = 30
    state_backend: str = "file"  # "file" en local, "ssm" en Lambda
    state_path: str = "state.json"
    state_ssm_param: str = "/mail2cal/state"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None, *, require_google: bool = True) -> Config:
        env = dict(os.environ if env is None else env)
        if env.get("SECRETS_SSM_PARAM"):
            env.update(load_ssm_secrets(env["SECRETS_SSM_PARAM"]))

        required = REQUIRED_IMAP + (REQUIRED_GOOGLE if require_google else ())
        missing = [k for k in required if not env.get(k)]
        if missing:
            raise ConfigError("Faltan variables de configuración: " + ", ".join(missing))

        tz = env.get("DEFAULT_TIMEZONE") or "UTC"
        try:
            zoneinfo.ZoneInfo(tz)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigError(f"DEFAULT_TIMEZONE no es una zona horaria válida: {tz!r}") from exc

        backend = env.get("STATE_BACKEND") or "file"
        if backend not in ("file", "ssm"):
            raise ConfigError(f"STATE_BACKEND debe ser 'file' o 'ssm', no {backend!r}")

        color = (env.get("EVENT_COLOR_ID") or "").strip()
        if color and color not in {str(i) for i in range(1, 12)}:
            raise ConfigError(f"EVENT_COLOR_ID debe ser un número del 1 al 11, no {color!r}")

        folders = tuple(f.strip() for f in (env.get("IMAP_FOLDERS") or "INBOX").split(",") if f.strip())

        return cls(
            imap_user=env["IMAP_USER"],
            imap_password=env["IMAP_PASSWORD"],
            imap_host=env.get("IMAP_HOST") or "imap.hostinger.com",
            imap_port=_int(env, "IMAP_PORT", 993),
            imap_folders=folders or ("INBOX",),
            google_client_id=env.get("GOOGLE_CLIENT_ID", ""),
            google_client_secret=env.get("GOOGLE_CLIENT_SECRET", ""),
            google_refresh_token=env.get("GOOGLE_REFRESH_TOKEN", ""),
            google_calendar_id=env.get("GOOGLE_CALENDAR_ID") or "primary",
            default_timezone=tz,
            event_color_id=color,
            backfill_days=_int(env, "BACKFILL_DAYS", 30),
            state_backend=backend,
            state_path=env.get("STATE_PATH") or "state.json",
            state_ssm_param=env.get("STATE_SSM_PARAM") or "/mail2cal/state",
        )


def _int(env: dict[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} debe ser un número entero, no {raw!r}") from exc


def load_ssm_secrets(name: str) -> dict[str, str]:
    """Lee un parámetro SecureString de SSM con un JSON {"VARIABLE": "valor"}."""
    import boto3  # incluido en el runtime de Lambda; solo hace falta en local si usas SSM

    resp = boto3.client("ssm").get_parameter(Name=name, WithDecryption=True)
    data = json.loads(resp["Parameter"]["Value"])
    return {str(k): str(v) for k, v in data.items()}


def load_dotenv(path: str | Path) -> None:
    """Carga KEY=valor de un archivo .env sin pisar variables ya definidas."""
    path = Path(path)
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)
