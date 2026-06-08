"""Lanceur de compatibilité : `python bot_logs.py`.

L'implémentation a été refactorée dans le paquet `bot/` (couches séparées).
Toute la configuration et tous les secrets sont chargés depuis `.env`
(voir `.env.example`) — plus aucune valeur sensible n'est codée en dur.
"""

from bot.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
