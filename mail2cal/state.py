"""Dónde se guarda el último mensaje procesado de cada carpeta."""
from __future__ import annotations

import json
import os
from pathlib import Path


class FileState:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    def save(self, state: dict) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


class SsmState:
    """Parámetro String de AWS SSM Parameter Store (gratis en el nivel estándar)."""

    def __init__(self, name: str, client=None):
        self.name = name
        if client is None:
            import boto3
            client = boto3.client("ssm")
        self.client = client

    def load(self) -> dict:
        try:
            resp = self.client.get_parameter(Name=self.name)
        except self.client.exceptions.ParameterNotFound:
            return {}
        return json.loads(resp["Parameter"]["Value"])

    def save(self, state: dict) -> None:
        self.client.put_parameter(Name=self.name, Value=json.dumps(state), Type="String", Overwrite=True)
