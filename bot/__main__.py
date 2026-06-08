"""Point d'entrée : `python -m bot`.

Charge la configuration, configure la journalisation (sans secrets) puis lance
le client Discord.
"""

from __future__ import annotations

import sys

from .config import Config, ConfigError
from .discord_app import BotLogsClient
from .logging_setup import setup_logging


def main() -> int:
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"[Configuration] {exc}", file=sys.stderr)
        return 2

    log = setup_logging(
        debug=config.debug,
        log_file=config.log_file,
        # Secrets explicitement masqués dans les logs (jamais affichés).
        secrets=[
            config.discord_token,
            config.wcl_client_secret,
            config.wcl_client_id,
        ],
    )
    log.info("Démarrage du bot (debug=%s).", config.debug)

    client = BotLogsClient(config)
    # log_handler=None : on garde notre configuration de logging (avec filtrage).
    client.run(config.discord_token, log_handler=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
