#!/usr/bin/env python3
"""Obtiene el refresh token de Google (se ejecuta una sola vez, en tu máquina).

Abre el navegador para que autorices el acceso a tu calendario y guarda el token
en GOOGLE_REFRESH_TOKEN dentro del .env. Lee GOOGLE_CLIENT_ID y GOOGLE_CLIENT_SECRET
del .env (cliente OAuth de tipo "App de escritorio").

    python3 scripts/get_google_token.py [--env-file .env]
"""
import argparse
import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

SCOPE = "https://www.googleapis.com/auth/calendar.events"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"


def read_env(path: Path) -> dict:
    env = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$", line)
            if m and not line.lstrip().startswith("#"):
                value = m.group(2)
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                env[m.group(1)] = value
    return {**env, **{k: v for k, v in os.environ.items() if k.startswith("GOOGLE_")}}


def write_token(path: Path, token: str) -> None:
    line = f"GOOGLE_REFRESH_TOKEN='{token}'"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if re.search(r"^GOOGLE_REFRESH_TOKEN=.*$", text, flags=re.M):
        text = re.sub(r"^GOOGLE_REFRESH_TOKEN=.*$", lambda _: line, text, flags=re.M)
    else:
        text = text.rstrip("\n") + ("\n" if text else "") + line + "\n"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", default=".env", type=Path)
    args = parser.parse_args()

    env = read_env(args.env_file)
    client_id, client_secret = env.get("GOOGLE_CLIENT_ID"), env.get("GOOGLE_CLIENT_SECRET")
    if not client_id or not client_secret:
        print(f"Faltan GOOGLE_CLIENT_ID y/o GOOGLE_CLIENT_SECRET en {args.env_file}", file=sys.stderr)
        return 1

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" not in query and "error" not in query:
                self.send_response(404)
                self.end_headers()
                return
            result.update({k: v[0] for k, v in query.items()})
            ok = "code" in query and query.get("state", [""])[0] == state
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = "Listo, ya puedes cerrar esta pestaña." if ok else "Algo falló; revisa la terminal."
            self.wfile.write(f"<p style='font-family:sans-serif'>{msg}</p>".encode())

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect_uri = f"http://127.0.0.1:{server.server_port}"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    print("Abre esta URL en el navegador (con tu cuenta personal de Google) y autoriza el acceso:\n")
    print(url + "\n")
    webbrowser.open(url)
    while not result:
        server.handle_request()
    server.server_close()

    if result.get("state") != state or "code" not in result:
        print(f"Autorización fallida: {result.get('error', 'state inválido')}", file=sys.stderr)
        return 1

    form = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "code": result["code"],
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=form, method="POST")) as resp:
            tokens = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        print(f"Google rechazó el código: {exc.read().decode()}", file=sys.stderr)
        return 1

    refresh = tokens.get("refresh_token")
    if not refresh:
        print("Google no devolvió refresh_token. Quita el acceso en https://myaccount.google.com/permissions"
              " y vuelve a intentarlo.", file=sys.stderr)
        return 1
    write_token(args.env_file, refresh)
    print(f"Refresh token guardado en {args.env_file} (GOOGLE_REFRESH_TOKEN).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
