"""bot_logs — bot Discord pour publier les runs Mythique+ / raid d'un groupe WoW.

Architecture (couches séparées) :
- config      : chargement de la configuration et des secrets (.env).
- wcl         : couche API Warcraft Logs (OAuth, GraphQL, cache jeton, retries).
- logic       : logique métier (extraction des runs, dédoublonnage, formatage).
- links       : génération des liens profonds (Warcraft Logs, WoWAnalyzer).
- db          : stockage local SQLite (anti-doublon, statistiques).
- discord_app : couche Discord (commandes, tags, embeds, boutons).
"""

__version__ = "2.0.0"
