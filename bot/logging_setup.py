"""Configuration de la journalisation, sans jamais écrire de secret.

Un filtre masque les valeurs sensibles connues (jeton Discord, secret WCL,
jetons OAuth) au cas où elles transiteraient malgré tout par un message de log.
"""

from __future__ import annotations

import logging
import os
import re
from logging.handlers import RotatingFileHandler

# Motifs systématiquement masqués dans les logs (ceinture + bretelles).
_TOKEN_PATTERNS = [
    re.compile(r"Bearer\s+[A-Za-z0-9\-_\.]+", re.IGNORECASE),
    re.compile(r'("?access_token"?\s*[:=]\s*)"?[A-Za-z0-9\-_\.]+"?', re.IGNORECASE),
    re.compile(r'("?client_secret"?\s*[:=]\s*)"?[A-Za-z0-9\-_\.]+"?', re.IGNORECASE),
]


class SecretRedactingFilter(logging.Filter):
    """Masque les secrets explicites et tout motif ressemblant à un jeton."""

    def __init__(self, secrets: list[str]):
        super().__init__()
        # On ne garde que les secrets non vides et suffisamment longs.
        self._secrets = [s for s in secrets if s and len(s) >= 6]

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True

        redacted = message
        for secret in self._secrets:
            if secret in redacted:
                redacted = redacted.replace(secret, "***")
        for pattern in _TOKEN_PATTERNS:
            redacted = pattern.sub("***", redacted)

        if redacted != message:
            # On réécrit le message déjà formaté pour neutraliser les args.
            record.msg = redacted
            record.args = ()
        return True


def setup_logging(
    *,
    debug: bool,
    log_file: str | None,
    secrets: list[str] | None = None,
) -> logging.Logger:
    """Configure le logger racine du bot et retourne le logger applicatif."""
    level = logging.DEBUG if debug else logging.INFO
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    redactor = SecretRedactingFilter(secrets or [])

    root = logging.getLogger()
    root.setLevel(level)
    # Évite les handlers dupliqués si setup_logging est appelé plusieurs fois.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.addFilter(redactor)
    root.addHandler(console)

    if log_file:
        os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        file_handler.addFilter(redactor)
        root.addHandler(file_handler)

    # discord.py est bavard en DEBUG : on le garde en INFO sauf debug explicite.
    logging.getLogger("discord").setLevel(logging.DEBUG if debug else logging.WARNING)

    return logging.getLogger("bot_logs")
