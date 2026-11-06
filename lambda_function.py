"""Punto de entrada de AWS Lambda (lo invoca EventBridge cada pocos minutos)."""
import logging

from mail2cal.config import Config
from mail2cal.sync import run

logging.getLogger().setLevel(logging.INFO)


def handler(event, context):
    return run(Config.from_env())
