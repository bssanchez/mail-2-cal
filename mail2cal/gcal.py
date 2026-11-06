"""Cliente mínimo de la API de Google Calendar v3 (solo biblioteca estándar)."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .ics_convert import time_key

log = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
API_URL = "https://www.googleapis.com/calendar/v3"
# Errores que dependen del evento concreto: se registran y se sigue con el resto.
PERMANENT_STATUSES = {400, 404, 409, 410}


class GoogleApiError(Exception):
    def __init__(self, status: int, detail: str, what: str = ""):
        super().__init__(f"Google Calendar respondió {status} en {what}: {detail[:500]}")
        self.status = status
        self.detail = detail

    @property
    def permanent(self) -> bool:
        return self.status in PERMANENT_STATUSES


class GoogleCalendar:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str, calendar_id: str = "primary"):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.calendar_id = calendar_id
        self._token: str | None = None
        self._token_expiry = 0.0

    # --- operaciones sobre eventos -------------------------------------------------

    def find_by_uid(self, uid: str) -> dict | None:
        """Evento principal con ese iCalUID (incluye eliminados, para poder restaurarlos)."""
        data = self._api("GET", "/events", params={"iCalUID": uid, "showDeleted": "true", "maxResults": 25})
        items = [e for e in data.get("items", []) if not e.get("recurringEventId")]
        items.sort(key=lambda e: e.get("status") == "cancelled")  # preferir los activos
        return items[0] if items else None

    def find_instance(self, event_id: str, original_start: dict) -> dict | None:
        """Instancia de un evento recurrente según su hora original (RECURRENCE-ID)."""
        target = time_key(original_start)
        path = f"/events/{quote(event_id, safe='')}/instances"
        if "dateTime" in original_start:
            utc = datetime.strptime(target, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            params = {"originalStart": utc.isoformat().replace("+00:00", "Z")}
            window = (utc - timedelta(days=2), utc + timedelta(days=2))
        else:
            params = {"originalStart": original_start["date"]}
            day = datetime.fromisoformat(original_start["date"]).replace(tzinfo=timezone.utc)
            window = (day - timedelta(days=2), day + timedelta(days=3))
        for query in (params, {"timeMin": _rfc3339(window[0]), "timeMax": _rfc3339(window[1])}):
            try:
                data = self._api("GET", path, params={**query, "showDeleted": "true", "maxResults": 50})
            except GoogleApiError as exc:
                if exc.permanent:
                    continue
                raise
            for item in data.get("items", []):
                original = item.get("originalStartTime")
                if original and time_key(original) == target:
                    return item
        return None

    def import_event(self, body: dict) -> dict:
        return self._api("POST", "/events/import", body=body)

    def update_event(self, event_id: str, body: dict) -> dict:
        return self._api("PUT", f"/events/{quote(event_id, safe='')}", params={"sendUpdates": "none"}, body=body)

    def patch_event(self, event_id: str, body: dict) -> dict:
        return self._api("PATCH", f"/events/{quote(event_id, safe='')}", params={"sendUpdates": "none"}, body=body)

    def delete_event(self, event_id: str) -> None:
        self._api("DELETE", f"/events/{quote(event_id, safe='')}", params={"sendUpdates": "none"})

    # --- HTTP ------------------------------------------------------------------------

    def _api(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None) -> dict:
        url = f"{API_URL}/calendars/{quote(self.calendar_id, safe='')}{path}"
        if params:
            url += "?" + urlencode(params)
        what = f"{method} {path}"
        data = json.dumps(body).encode() if body is not None else None
        last_error: GoogleApiError | None = None
        for attempt in range(4):
            if attempt:
                time.sleep(2 ** attempt)
            headers = {"Authorization": f"Bearer {self._access_token()}", "Accept": "application/json"}
            if data is not None:
                headers["Content-Type"] = "application/json"
            try:
                with urlopen(Request(url, data=data, method=method, headers=headers), timeout=30) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else {}
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                last_error = GoogleApiError(exc.code, detail, what)
                if exc.code == 401:
                    self._token = None
                    continue
                if exc.code in (429, 500, 502, 503, 504) or (exc.code == 403 and "ateLimitExceeded" in detail):
                    continue
                raise last_error from None
            except (URLError, TimeoutError) as exc:
                last_error = GoogleApiError(0, str(exc), what)
        raise last_error

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        form = urlencode({
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
            "grant_type": "refresh_token",
        }).encode()
        try:
            with urlopen(Request(TOKEN_URL, data=form, method="POST"), timeout=30) as resp:
                payload = json.loads(resp.read())
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            hint = ""
            if "invalid_grant" in detail:
                hint = (" El refresh token caducó o fue revocado. Si la pantalla de consentimiento de OAuth"
                        " está en modo 'Testing', los tokens duran 7 días: publícala ('In production') y"
                        " vuelve a ejecutar scripts/get_google_token.py.")
            raise GoogleApiError(401, detail + hint, "renovar token de Google") from None
        self._token = payload["access_token"]
        self._token_expiry = time.time() + int(payload.get("expires_in", 3600))
        return self._token


def _rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
