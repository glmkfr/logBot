"""Logique métier : extraction des runs, sélection des pulls, formatage.

Cette couche ne connaît ni Discord ni le réseau : elle transforme un rapport
WCL brut (dict GraphQL) en structures exploitables, de façon défensive — si un
champ manque, on dégrade proprement plutôt que de crasher.
"""

from __future__ import annotations

import datetime
import unicodedata
from dataclasses import dataclass, field

# --- Abréviations des donjons / instances ---
# Clé : nom complet (en minuscules), Valeur : abréviation à afficher.
# Si un donjon n'est pas listé ici, son nom complet est utilisé tel quel.
ABBREVIATIONS = {
    "the seat of the triumvirate": "SEAT",
    "algeth'ar academy": "AA",
    "windrunner spire": "WS",
    "skyreach": "SKY",
    "maisara caverns": "MC",
    "pit of saron": "PoS",
    "magister terrace": "MT",
    "nexus-point xenas": "NPX",
}

# Mapping (partiel, best-effort) des IDs d'affixes M+ vers leur nom.
# Les IDs inconnus sont rendus sous la forme "Affixe #<id>" (dégradation propre).
AFFIX_NAMES = {
    2: "Renforcé",
    3: "Volcanique",
    4: "Nécrotique",
    6: "Sanglant",
    7: "Boueux",
    8: "Sismique",
    9: "Pillé",
    10: "Fortifié",
    11: "Brutal",
    12: "Grégaire",
    13: "Explosif",
    14: "Quaking",
    122: "Inspiré",
    123: "Spiteful",
    124: "Storming",
    134: "Entêté",
    135: "Foudroyé",
    136: "Déferlant",
    147: "Tracas",
    148: "Affolé",
    158: "Vicieux",
    159: "Sanguine",
    160: "Surdimensionné",
    162: "Pulsé",
    163: "Attisé",
    1001: "Glacée",
    1002: "Putride",
    1003: "Funeste",
    1004: "Entropique",
    1005: "Asservissant",
    1006: "Sanglant",
    1007: "Vénéneux",
}


def abbreviate(name: str) -> str:
    """Renvoie l'abréviation d'une instance, ou son nom tel quel si inconnue."""
    return ABBREVIATIONS.get((name or "").lower().strip(), name)


def affix_label(affix_id: int) -> str:
    """Nom lisible d'un affixe (best-effort)."""
    return AFFIX_NAMES.get(affix_id, f"Affixe #{affix_id}")


def format_duration(milliseconds: int | None) -> str | None:
    """Formate une durée en ms vers 'MM:SS' (ou 'H:MM:SS'). None si absent."""
    if not milliseconds or milliseconds <= 0:
        return None
    total_seconds = int(milliseconds // 1000)
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def report_date(report: dict) -> str:
    """Date du rapport au format JJ/MM (repli sur aujourd'hui si absente)."""
    start_ms = report.get("startTime")
    if start_ms:
        return datetime.datetime.fromtimestamp(start_ms / 1000).strftime("%d/%m")
    return datetime.datetime.now().strftime("%d/%m")


def report_time_label(report: dict, fight: dict) -> str:
    """Heure de début du combat (HH:MM) pour désambiguïser les titres."""
    base = report.get("startTime") or 0
    offset = fight.get("startTime") or 0
    if base:
        ts = (base + offset) / 1000
        return datetime.datetime.fromtimestamp(ts).strftime("%Hh%M")
    return ""


# --------------------------------------------------------------------------- #
# Composition (classes des joueurs présents sur un combat)
# --------------------------------------------------------------------------- #

def _actors_by_id(report: dict) -> dict[int, dict]:
    actors = (report.get("masterData") or {}).get("actors") or []
    return {a["id"]: a for a in actors if a.get("id") is not None}


def composition(report: dict, fight: dict) -> list[str]:
    """Liste des classes présentes sur un combat (depuis friendlyPlayers).

    Retourne une liste vide si l'info n'est pas exploitable (dégradation propre).
    """
    actors = _actors_by_id(report)
    player_ids = fight.get("friendlyPlayers") or []
    classes: list[str] = []
    for pid in player_ids:
        actor = actors.get(pid)
        if actor and actor.get("subType"):
            classes.append(actor["subType"])
    return classes


def composition_names(report: dict, fight: dict) -> list[tuple[str, str | None]]:
    """Liste (nom du personnage, classe) des joueurs présents sur un combat.

    Retourne une liste vide si l'info n'est pas exploitable (dégradation propre).
    Utilisé pour persister le roster d'un run (cf. /leaderboard compétitif).
    """
    actors = _actors_by_id(report)
    player_ids = fight.get("friendlyPlayers") or []
    players: list[tuple[str, str | None]] = []
    for pid in player_ids:
        actor = actors.get(pid)
        if actor and actor.get("name"):
            players.append((actor["name"], actor.get("subType")))
    return players


def normalize_character(name: str) -> str:
    """Clé de comparaison d'un nom de perso : sans royaume, sans accents, minuscule.

    Warcraft Logs renvoie parfois « Nom-Royaume » ; on ne garde que le nom et on
    retire les diacritiques pour permettre l'association manuelle et l'auto-match
    avec les pseudos Discord malgré les variations d'écriture.
    """
    base = (name or "").split("-", 1)[0].strip().lower()
    decomposed = unicodedata.normalize("NFKD", base)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def composition_summary(report: dict, fight: dict) -> str | None:
    """Résumé compact de la composition, ex: '2x Mage, Priest, Warrior'."""
    classes = composition(report, fight)
    if not classes:
        return None
    counts: dict[str, int] = {}
    for c in classes:
        counts[c] = counts.get(c, 0) + 1
    parts = [
        f"{n}x {name}" if n > 1 else name
        for name, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return ", ".join(parts)


# --------------------------------------------------------------------------- #
# Mythique+
# --------------------------------------------------------------------------- #

@dataclass
class KeystoneRun:
    """Un run Mythique+ extrait d'un rapport."""

    report_code: str
    fight_id: int
    dungeon: str
    level: int
    timed: bool
    keystone_time_ms: int | None
    bonus: int  # nombre de paliers (+1/+2/+3) gagnés ; >0 = timé
    affixes: list[int] = field(default_factory=list)
    date: str = ""
    time_label: str = ""
    item_level: float | None = None  # iLvl moyen du groupe (None si absent)

    @property
    def dungeon_abbr(self) -> str:
        return abbreviate(self.dungeon)

    @property
    def status_label(self) -> str:
        return "Timé" if self.timed else "Non timé"


def extract_keystone_runs(report: dict) -> list[KeystoneRun]:
    """Retourne tous les runs M+ d'un rapport (un par combat avec keystoneLevel)."""
    code = report.get("code") or ""
    date = report_date(report)
    runs: list[KeystoneRun] = []

    for f in report.get("fights") or []:
        level = f.get("keystoneLevel")
        if not level:
            continue
        dungeon = (f.get("gameZone") or {}).get("name") or "Donjon"
        bonus = f.get("keystoneBonus") or 0
        runs.append(
            KeystoneRun(
                report_code=code,
                fight_id=f.get("id"),
                dungeon=dungeon,
                level=int(level),
                timed=bonus > 0,
                keystone_time_ms=f.get("keystoneTime"),
                bonus=int(bonus),
                affixes=[a for a in (f.get("keystoneAffixes") or []) if a],
                date=date,
                time_label=report_time_label(report, f),
                item_level=f.get("averageItemLevel"),
            )
        )
    return runs


def build_titles(runs: list[KeystoneRun]) -> dict[int, str]:
    """Construit les titres de fil, en désambiguïsant les doublons du jour.

    Clé du dict = fight_id. Si deux runs partagent (donjon, niveau, date), on
    ajoute l'heure pour les distinguer ; à défaut d'heure, un index.
    """
    # Regroupe par (donjon abrégé, niveau, date) pour repérer les collisions.
    groups: dict[tuple, list[KeystoneRun]] = {}
    for r in runs:
        groups.setdefault((r.dungeon_abbr, r.level, r.date), []).append(r)

    titles: dict[int, str] = {}
    for (abbr, level, date), group in groups.items():
        if len(group) == 1:
            r = group[0]
            titles[r.fight_id] = f"{abbr} +{level} — {date}"[:100]
            continue
        # Collision : désambiguïse par l'heure, sinon par un index.
        for idx, r in enumerate(group, start=1):
            suffix = r.time_label or f"#{idx}"
            titles[r.fight_id] = f"{abbr} +{level} — {date} ({suffix})"[:100]
    return titles


def affixes_summary(run: KeystoneRun) -> str | None:
    """Résumé lisible des affixes d'un run, ou None si aucun."""
    if not run.affixes:
        return None
    return ", ".join(affix_label(a) for a in run.affixes)


# --------------------------------------------------------------------------- #
# Raid
# --------------------------------------------------------------------------- #

@dataclass
class RaidEncounter:
    """Un boss de raid avec son pull représentatif (kill ou meilleur essai)."""

    encounter_id: int
    name: str
    fight_id: int
    killed: bool
    pulls: int
    best_percentage: float | None  # % de vie restante du boss au meilleur essai


def extract_raid_encounters(report: dict) -> list[RaidEncounter]:
    """Regroupe les combats de raid par boss et choisit le pull représentatif.

    Pull représentatif = le kill ; à défaut, l'essai le plus proche du kill
    (fightPercentage le plus bas). Les combats M+ (keystoneLevel) sont ignorés.
    """
    by_encounter: dict[int, list[dict]] = {}
    for f in report.get("fights") or []:
        if f.get("keystoneLevel"):
            continue  # c'est du M+, pas un boss de raid
        enc = f.get("encounterID")
        if not enc:
            continue  # trash / pull non identifié à un boss
        by_encounter.setdefault(enc, []).append(f)

    encounters: list[RaidEncounter] = []
    for enc, fights in by_encounter.items():
        kills = [f for f in fights if f.get("kill")]
        if kills:
            representative = kills[0]
            killed = True
        else:
            # Meilleur essai = fightPercentage le plus bas (boss le plus entamé).
            representative = min(
                fights,
                key=lambda f: f.get("fightPercentage")
                if f.get("fightPercentage") is not None
                else 100.0,
            )
            killed = False

        encounters.append(
            RaidEncounter(
                encounter_id=enc,
                name=representative.get("name") or f"Boss {enc}",
                fight_id=representative.get("id"),
                killed=killed,
                pulls=len(fights),
                best_percentage=representative.get("fightPercentage"),
            )
        )

    # Ordre d'apparition (par fight_id croissant) pour un rendu cohérent.
    encounters.sort(key=lambda e: e.fight_id if e.fight_id is not None else 0)
    return encounters


def raid_zone_name(report: dict) -> str:
    """Nom de la zone de raid d'un rapport (repli générique)."""
    return (report.get("zone") or {}).get("name") or "Raid"
