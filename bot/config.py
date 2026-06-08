"""Chargement de la configuration et des secrets depuis l'environnement / `.env`.

Aucune valeur sensible n'est codée en dur : tout passe par les variables
d'environnement, chargées via python-dotenv. La validation échoue tôt et avec
un message clair si une variable obligatoire manque.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

# Charge le fichier .env du répertoire courant (sans écraser l'environnement
# déjà défini, pour rester compatible avec systemd/docker qui injectent l'env).
load_dotenv(override=False)


class ConfigError(RuntimeError):
    """Erreur de configuration (variable manquante ou invalide)."""


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"Variable d'environnement obligatoire manquante : {name}. "
            f"Voir .env.example."
        )
    return value


def _require_int(name: str) -> int:
    raw = _require(name)
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} doit être un entier (reçu : {raw!r}).") from exc


def _optional_int(name: str, default: int | None = None) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} doit être un entier (reçu : {raw!r}).") from exc


def _id_list(name: str) -> list[int]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return []
    ids: list[int] = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part:
            try:
                ids.append(int(part))
            except ValueError as exc:
                raise ConfigError(
                    f"{name} doit être une liste d'IDs entiers séparés par des "
                    f"virgules (élément invalide : {part!r})."
                ) from exc
    return ids


@dataclass(frozen=True)
class Config:
    """Configuration immuable du bot."""

    discord_token: str
    guild_id: int
    forum_channel_id: int
    wcl_client_id: str
    wcl_client_secret: str

    debug: bool = False
    min_key_level: int = 2
    allowed_role_ids: list[int] = field(default_factory=list)
    database_path: str = "data/bot_logs.db"
    log_channel_id: int | None = None
    log_file: str | None = "logs/bot.log"
    wowanalyzer_raid_links: bool = True

    # Récap hebdomadaire automatique (désactivé si recap_channel_id est None).
    recap_channel_id: int | None = None
    recap_weekday: int = 0  # 0 = lundi … 6 = dimanche
    recap_hour: int = 10    # heure locale du serveur

    @classmethod
    def from_env(cls) -> "Config":
        """Construit la configuration en validant les variables obligatoires."""
        return cls(
            discord_token=_require("DISCORD_TOKEN"),
            guild_id=_require_int("GUILD_ID"),
            forum_channel_id=_require_int("FORUM_CHANNEL_ID"),
            wcl_client_id=_require("WCL_CLIENT_ID"),
            wcl_client_secret=_require("WCL_CLIENT_SECRET"),
            debug=os.environ.get("DEBUG", "0").strip() in {"1", "true", "True"},
            min_key_level=_optional_int("MIN_KEY_LEVEL", 2) or 2,
            allowed_role_ids=_id_list("ALLOWED_ROLE_IDS"),
            database_path=os.environ.get("DATABASE_PATH", "data/bot_logs.db").strip()
            or "data/bot_logs.db",
            log_channel_id=_optional_int("LOG_CHANNEL_ID", None),
            log_file=(os.environ.get("LOG_FILE", "logs/bot.log").strip() or None),
            wowanalyzer_raid_links=os.environ.get(
                "WOWANALYZER_RAID_LINKS", "1"
            ).strip()
            in {"1", "true", "True"},
            recap_channel_id=_optional_int("RECAP_CHANNEL_ID", None),
            recap_weekday=min(max(_optional_int("RECAP_WEEKDAY", 0) or 0, 0), 6),
            recap_hour=min(max(_optional_int("RECAP_HOUR", 10) or 10, 0), 23),
        )
