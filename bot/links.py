"""Génération des liens profonds (Warcraft Logs et WoWAnalyzer).

On ne fait QUE construire des URLs vers les interfaces web : aucune extraction
ni récupération programmatique d'analyse (cf. brief §6 et §9.3 — aucune API de
suggestions WoWAnalyzer n'est supposée exister).

Formats vérifiés :
- Warcraft Logs (combat précis) : https://www.warcraftlogs.com/reports/<code>#fight=<id>
- WoWAnalyzer (rapport + combat)  : https://wowanalyzer.com/report/<code>/<fightId>
  La sélection du joueur est laissée à l'utilisateur sur la page (cf. brief §6).
"""

from __future__ import annotations

import re

WCL_REPORT_BASE = "https://www.warcraftlogs.com/reports"
WOWANALYZER_BASE = "https://wowanalyzer.com/report"

# Code de rapport présent dans une URL warcraftlogs.com/reports/<code>...
REPORT_CODE_RE = re.compile(r"/reports/([a-zA-Z0-9]+)")
# Repère les URLs (http/https) dans un texte libre, pour l'auto-détection.
_URL_RE = re.compile(r"https?://\S+")
# Domaines acceptés pour la validation du lien fourni à /logs.
# Domaine racine accepté. On autorise le domaine nu et TOUT sous-domaine
# (www., fr., de., ko., etc.), mais jamais un faux domaine du type
# « fakewarcraftlogs.com » ou « warcraftlogs.com.evil.com ».
_ALLOWED_ROOT = "warcraftlogs.com"


def extract_report_code(url: str) -> str | None:
    """Extrait le code de rapport d'une URL Warcraft Logs, ou None si absent."""
    if not url:
        return None
    match = REPORT_CODE_RE.search(url)
    return match.group(1) if match else None


def is_warcraftlogs_url(url: str) -> bool:
    """Valide que l'URL pointe bien vers warcraftlogs.com (cf. brief §7)."""
    if not url:
        return False
    # Validation simple et robuste sans dépendre du schéma exact.
    from urllib.parse import urlparse

    candidate = url.strip()
    if not candidate.lower().startswith(("http://", "https://")):
        candidate = "https://" + candidate
    try:
        host = (urlparse(candidate).hostname or "").lower()
    except ValueError:
        return False
    return host == _ALLOWED_ROOT or host.endswith("." + _ALLOWED_ROOT)


def find_warcraftlogs_url(text: str) -> str | None:
    """Trouve le premier lien Warcraft Logs valide dans un texte libre.

    Sert à l'auto-détection : on isole chaque URL du message, on retire la
    ponctuation de fin (« …/abc). », « <…/abc> ») puis on revalide le domaine
    via `is_warcraftlogs_url` pour ne jamais matcher un faux domaine
    (« fakewarcraftlogs.com »). Retourne None si aucun lien exploitable.
    """
    if not text:
        return None
    for token in _URL_RE.findall(text):
        candidate = token.strip().rstrip(").,;!?>\"'")
        if is_warcraftlogs_url(candidate) and extract_report_code(candidate):
            return candidate
    return None


def wcl_fight_url(code: str, fight_id: int | None) -> str:
    """Lien Warcraft Logs vers un combat précis (ou le rapport si fight inconnu)."""
    if fight_id is None:
        return f"{WCL_REPORT_BASE}/{code}"
    return f"{WCL_REPORT_BASE}/{code}#fight={fight_id}"


def wowanalyzer_url(code: str, fight_id: int | None = None) -> str:
    """Lien WoWAnalyzer vers le rapport (et le combat si fourni)."""
    if fight_id is None:
        return f"{WOWANALYZER_BASE}/{code}"
    return f"{WOWANALYZER_BASE}/{code}/{fight_id}"
