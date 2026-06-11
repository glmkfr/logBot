"""Backfill du roster (table `run_players`) des runs M+ déjà enregistrés.

Les runs publiés avant l'ajout de la feature « leaderboard compétitif » n'ont
pas de roster en base : leurs joueurs n'apparaissent donc pas sur `/leaderboard`.
Ce script ré-interroge Warcraft Logs pour chaque rapport concerné et renseigne
les personnages manquants. Il est **idempotent** (INSERT OR IGNORE) : on peut le
relancer sans risque, il ne retraite que les runs sans roster.

Usage (depuis la racine du dépôt, avec le `.env` configuré) :

    python scripts/backfill_run_players.py
    # ou via le venv : .venv/bin/python scripts/backfill_run_players.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections import defaultdict

# Permet d'importer le paquet `bot` quand le script est lancé directement.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import logic  # noqa: E402
from bot.config import Config, ConfigError  # noqa: E402
from bot.db import Database  # noqa: E402
from bot.wcl import WarcraftLogsClient, WCLError  # noqa: E402


def _find_fight(report: dict, fight_id: int) -> dict:
    """Retourne le combat d'`id` donné dans un rapport (ou {} s'il est absent)."""
    for fight in report.get("fights") or []:
        if fight.get("id") == fight_id:
            return fight
    return {}


async def run() -> None:
    config = Config.from_env()
    db = Database(config.database_path)

    missing = db.runs_without_roster()
    if not missing:
        print("Rien à backfiller : tous les runs M+ ont déjà un roster.")
        db.close()
        return

    # Un rapport peut contenir plusieurs runs : on le télécharge une seule fois.
    by_report: dict[str, list[int]] = defaultdict(list)
    for code, fight_id in missing:
        by_report[code].append(fight_id)

    print(
        f"{len(missing)} run(s) sans roster répartis sur {len(by_report)} "
        f"rapport(s). Récupération depuis Warcraft Logs…"
    )

    filled = skipped = errors = 0
    async with WarcraftLogsClient(
        config.wcl_client_id, config.wcl_client_secret
    ) as wcl:
        for code, fight_ids in by_report.items():
            try:
                report = await wcl.fetch_report(code)
            except WCLError as exc:
                print(f"  ⚠️  {code} : erreur Warcraft Logs ({exc}) — ignoré.")
                errors += len(fight_ids)
                continue
            except Exception as exc:  # noqa: BLE001
                print(f"  ⚠️  {code} : erreur inattendue ({exc}) — ignoré.")
                errors += len(fight_ids)
                continue

            if not report:
                print(f"  ⚠️  {code} : rapport introuvable ou privé — ignoré.")
                skipped += len(fight_ids)
                continue

            done = 0
            for fight_id in fight_ids:
                players = logic.composition_names(report, _find_fight(report, fight_id))
                if players:
                    db.record_run_players(code, fight_id, players)
                    filled += 1
                    done += 1
                else:
                    skipped += 1
            print(f"  ✓ {code} : {done}/{len(fight_ids)} run(s) renseigné(s).")

    print(
        f"Terminé. {filled} run(s) renseigné(s), {skipped} sans joueurs "
        f"exploitables, {errors} en erreur."
    )
    db.close()


def main() -> int:
    try:
        asyncio.run(run())
    except ConfigError as exc:
        print(f"[Configuration] {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
