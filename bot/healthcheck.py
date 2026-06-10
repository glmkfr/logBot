"""Healthcheck Docker : `python -m bot.healthcheck`.

Vérifie que le bot a touché récemment son fichier de battement de cœur
(`HEARTBEAT_FILE`, écrit chaque minute tant que la passerelle Discord est
saine). Sort 0 si le fichier est frais, 1 sinon — ce qui marque le conteneur
*unhealthy* (visible dans `docker ps` ; combinable avec un autoheal pour
redémarrer automatiquement).
"""

from __future__ import annotations

import os
import sys
import time

# Tolérance par défaut : 3 min (le bot écrit toutes les minutes). Surchargée par
# HEARTBEAT_MAX_AGE si besoin.
DEFAULT_MAX_AGE = 180


def main() -> int:
    path = os.environ.get("HEARTBEAT_FILE", "data/heartbeat").strip() or "data/heartbeat"
    try:
        max_age = int(os.environ.get("HEARTBEAT_MAX_AGE", DEFAULT_MAX_AGE))
    except ValueError:
        max_age = DEFAULT_MAX_AGE
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        print(f"heartbeat absent: {path}", file=sys.stderr)
        return 1
    if age > max_age:
        print(f"heartbeat périmé: {age:.0f}s > {max_age}s", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
