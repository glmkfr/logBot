"""Stockage local SQLite : anti-doublon des runs et données pour /stats.

Clé d'unicité d'un run = (report_code, fight_id) — cf. brief §4.
Le module est volontairement synchrone (sqlite3) mais ses appels sont courts ;
ils sont exécutés depuis la couche Discord via asyncio.to_thread pour ne pas
bloquer la boucle d'événements. La connexion étant alors partagée entre threads
du pool, elle est ouverte avec check_same_thread=False et tous les accès sont
sérialisés par un verrou (self._lock).
"""

from __future__ import annotations

import datetime
import os
import sqlite3
import threading
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    report_code   TEXT    NOT NULL,
    fight_id      INTEGER NOT NULL,
    kind          TEXT    NOT NULL DEFAULT 'mplus',  -- 'mplus' | 'raid'
    dungeon       TEXT,
    level         INTEGER,
    timed         INTEGER,            -- 0/1
    keystone_time INTEGER,            -- ms
    encounter_id  INTEGER,
    date          TEXT,               -- JJ/MM (affichage)
    thread_id     INTEGER,
    created_at    TEXT NOT NULL,
    UNIQUE(report_code, fight_id)
);

CREATE TABLE IF NOT EXISTS raid_threads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    report_code TEXT NOT NULL,
    zone        TEXT,
    thread_id   INTEGER,
    created_at  TEXT NOT NULL,
    UNIQUE(report_code, zone)
);

-- Liens ajoutés a posteriori (route / VoD) par fil, persistés pour survivre
-- au redémarrage. kind = 'route' | 'vod'.
CREATE TABLE IF NOT EXISTS thread_links (
    thread_id  INTEGER NOT NULL,
    kind       TEXT    NOT NULL,
    url        TEXT    NOT NULL,
    updated_at TEXT    NOT NULL,
    PRIMARY KEY (thread_id, kind)
);

-- Roster d'un run : un personnage WoW par ligne (pour le /leaderboard
-- compétitif). character_name est le nom brut renvoyé par Warcraft Logs.
CREATE TABLE IF NOT EXISTS run_players (
    report_code    TEXT    NOT NULL,
    fight_id       INTEGER NOT NULL,
    character_name TEXT    NOT NULL,
    class          TEXT,
    PRIMARY KEY (report_code, fight_id, character_name)
);

-- Association manuelle perso WoW -> membre Discord (commande /lier).
-- character_key = nom de perso normalisé (sans royaume, sans accents, minuscule).
CREATE TABLE IF NOT EXISTS member_links (
    character_key   TEXT    PRIMARY KEY,
    discord_user_id INTEGER NOT NULL,
    added_at        TEXT    NOT NULL
);
"""


@dataclass
class Stats:
    total: int
    timed: int
    avg_level: float
    by_dungeon: dict[str, int]
    # Meilleure clé timée (niveau le plus haut) et son donjon. None si aucune.
    best_level: int | None = None
    best_dungeon: str | None = None

    @property
    def timed_pct(self) -> float:
        return (self.timed / self.total * 100.0) if self.total else 0.0


@dataclass
class LeaderboardEntry:
    """Meilleure performance timée d'un donjon (pour /leaderboard)."""

    dungeon: str
    best_level: int
    best_time_ms: int | None  # meilleur temps au niveau record (None si absent)
    timed_count: int          # nombre total de clés timées sur ce donjon
    # Run précis qui détient le record (pour retrouver son roster). None si
    # indéterminable (ne devrait pas arriver pour une entrée présente).
    report_code: str | None = None
    fight_id: int | None = None


class Database:
    """Accès SQLite minimal et défensif."""

    def __init__(self, path: str):
        self.path = path
        if os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        # check_same_thread=False : la connexion est utilisée depuis le pool de
        # threads d'asyncio.to_thread. On sérialise tous les accès via _lock
        # pour rester thread-safe malgré ce partage.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _now() -> str:
        return datetime.datetime.now().isoformat(timespec="seconds")

    # -- Anti-doublon ---------------------------------------------------------

    def run_exists(self, report_code: str, fight_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM runs WHERE report_code = ? AND fight_id = ?",
                (report_code, fight_id),
            )
            return cur.fetchone() is not None

    def record_run(
        self,
        *,
        report_code: str,
        fight_id: int,
        kind: str,
        dungeon: str | None,
        level: int | None,
        timed: bool | None,
        keystone_time: int | None,
        encounter_id: int | None,
        date: str | None,
        thread_id: int | None,
    ) -> bool:
        """Enregistre un run. Retourne False si (report_code, fight_id) existe déjà."""
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO runs (report_code, fight_id, kind, dungeon, level,
                                      timed, keystone_time, encounter_id, date,
                                      thread_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        report_code,
                        fight_id,
                        kind,
                        dungeon,
                        level,
                        None if timed is None else int(timed),
                        keystone_time,
                        encounter_id,
                        date,
                        thread_id,
                        self._now(),
                    ),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    # -- Roster d'un run ------------------------------------------------------

    def record_run_players(
        self, report_code: str, fight_id: int, players: list[tuple[str, str | None]]
    ) -> None:
        """Enregistre le roster d'un run (best-effort, idempotent).

        `players` est une liste de (nom_du_perso, classe). Les doublons et les
        ré-enregistrements sont ignorés silencieusement (INSERT OR IGNORE).
        """
        if not players:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT OR IGNORE INTO run_players "
                "(report_code, fight_id, character_name, class) VALUES (?, ?, ?, ?)",
                [(report_code, fight_id, name, cls) for name, cls in players if name],
            )
            self._conn.commit()

    def runs_without_roster(self) -> list[tuple[str, int]]:
        """Runs M+ sans roster enregistré (pour le backfill). (report_code, fight_id)."""
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT r.report_code, r.fight_id FROM runs r
                WHERE r.kind = 'mplus'
                  AND NOT EXISTS (
                      SELECT 1 FROM run_players p
                      WHERE p.report_code = r.report_code
                        AND p.fight_id = r.fight_id
                  )
                ORDER BY r.report_code, r.fight_id
                """
            )
            return [(row["report_code"], row["fight_id"]) for row in cur.fetchall()]

    def get_run_players(
        self, report_code: str, fight_id: int
    ) -> list[tuple[str, str | None]]:
        """Retourne le roster (nom, classe) enregistré pour un run."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT character_name, class FROM run_players "
                "WHERE report_code = ? AND fight_id = ? ORDER BY character_name",
                (report_code, fight_id),
            )
            return [(r["character_name"], r["class"]) for r in cur.fetchall()]

    def player_run_rows(self, since_iso: str | None = None) -> list[sqlite3.Row]:
        """Lignes (joueur × run M+) pour les stats joueurs.

        Chaque ligne joint un personnage de `run_players` à son run M+ :
        report_code, fight_id, character_name, dungeon, level, timed,
        keystone_time. `since_iso` limite aux runs publiés depuis cette date.
        Les Row renvoyés sont détachés de la connexion (utilisables hors verrou).
        """
        query = (
            "SELECT p.report_code, p.fight_id, p.character_name, "
            "       r.dungeon, r.level, r.timed, r.keystone_time "
            "FROM run_players p "
            "JOIN runs r ON r.report_code = p.report_code "
            "          AND r.fight_id = p.fight_id "
            "WHERE r.kind = 'mplus'"
        )
        params: tuple = ()
        if since_iso:
            query += " AND r.created_at >= ?"
            params = (since_iso,)
        with self._lock:
            return self._conn.execute(query, params).fetchall()

    # -- Liaison perso WoW <-> membre Discord ---------------------------------

    def link_character(self, character_key: str, discord_user_id: int) -> None:
        """Associe un perso (clé normalisée) à un membre Discord (upsert)."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO member_links (character_key, discord_user_id, added_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(character_key) DO UPDATE SET "
                "discord_user_id = excluded.discord_user_id, added_at = excluded.added_at",
                (character_key, discord_user_id, self._now()),
            )
            self._conn.commit()

    def unlink_character(self, character_key: str, discord_user_id: int) -> bool:
        """Supprime un lien si (et seulement si) il appartient à ce membre."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM member_links "
                "WHERE character_key = ? AND discord_user_id = ?",
                (character_key, discord_user_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def get_character_link(self, character_key: str) -> int | None:
        """Retourne l'ID Discord lié à un perso, ou None."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT discord_user_id FROM member_links WHERE character_key = ?",
                (character_key,),
            )
            row = cur.fetchone()
            return row["discord_user_id"] if row else None

    def all_character_links(self) -> dict[str, int]:
        """Retourne tout le mapping {clé_perso: discord_user_id}."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT character_key, discord_user_id FROM member_links"
            )
            return {r["character_key"]: r["discord_user_id"] for r in cur.fetchall()}

    def get_links_for_user(self, discord_user_id: int) -> list[str]:
        """Retourne les clés de perso liées à un membre."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT character_key FROM member_links "
                "WHERE discord_user_id = ? ORDER BY character_key",
                (discord_user_id,),
            )
            return [r["character_key"] for r in cur.fetchall()]

    # -- Fils de raid ---------------------------------------------------------

    def get_raid_thread(self, report_code: str, zone: str) -> int | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT thread_id FROM raid_threads WHERE report_code = ? AND zone = ?",
                (report_code, zone),
            )
            row = cur.fetchone()
            return row["thread_id"] if row else None

    def record_raid_thread(
        self, report_code: str, zone: str, thread_id: int
    ) -> bool:
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO raid_threads (report_code, zone, thread_id, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (report_code, zone, thread_id, self._now()),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    # -- Liens route / VoD ----------------------------------------------------

    def set_thread_link(self, thread_id: int, kind: str, url: str) -> None:
        """Enregistre (ou met à jour) un lien route/VoD pour un fil."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO thread_links (thread_id, kind, url, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(thread_id, kind) DO UPDATE SET "
                "url = excluded.url, updated_at = excluded.updated_at",
                (thread_id, kind, url, self._now()),
            )
            self._conn.commit()

    def get_thread_links(self, thread_id: int) -> dict[str, str]:
        """Retourne {kind: url} des liens enregistrés pour un fil."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT kind, url FROM thread_links WHERE thread_id = ?",
                (thread_id,),
            )
            return {r["kind"]: r["url"] for r in cur.fetchall()}

    # -- Statistiques ---------------------------------------------------------

    def stats(self, since_iso: str | None = None) -> Stats:
        """Statistiques M+ globales (optionnellement depuis une date ISO)."""
        where = "WHERE kind = 'mplus'"
        params: tuple = ()
        if since_iso:
            where += " AND created_at >= ?"
            params = (since_iso,)

        with self._lock:
            cur = self._conn.execute(
                f"SELECT COUNT(*) AS n, "
                f"SUM(CASE WHEN timed = 1 THEN 1 ELSE 0 END) AS timed, "
                f"AVG(level) AS avg_level FROM runs {where}",
                params,
            )
            row = cur.fetchone()
            total = row["n"] or 0
            timed = row["timed"] or 0
            avg_level = float(row["avg_level"] or 0.0)

            cur = self._conn.execute(
                f"SELECT dungeon, COUNT(*) AS n FROM runs {where} "
                f"GROUP BY dungeon ORDER BY n DESC",
                params,
            )
            by_dungeon = {r["dungeon"] or "?": r["n"] for r in cur.fetchall()}

            # Meilleure clé timée de la période (niveau le plus haut).
            cur = self._conn.execute(
                f"SELECT dungeon, level FROM runs {where} AND timed = 1 "
                f"ORDER BY level DESC LIMIT 1",
                params,
            )
            best = cur.fetchone()
            best_level = best["level"] if best else None
            best_dungeon = (best["dungeon"] if best else None) or None

        return Stats(
            total=total,
            timed=timed,
            avg_level=avg_level,
            by_dungeon=by_dungeon,
            best_level=best_level,
            best_dungeon=best_dungeon,
        )

    def leaderboard(self) -> list[LeaderboardEntry]:
        """Classement : meilleure clé timée par donjon (niveau, puis temps).

        Pour chaque donjon, on retient le niveau timé le plus élevé jamais
        publié, le meilleur (plus court) temps à ce niveau et le nombre total de
        clés timées. Les donjons sans aucune clé timée sont absents.
        """
        query = """
            SELECT r.dungeon                AS dungeon,
                   r.level                  AS best_level,
                   MIN(r.keystone_time)     AS best_time,
                   (SELECT COUNT(*) FROM runs c
                     WHERE c.kind = 'mplus' AND c.timed = 1
                       AND c.dungeon IS r.dungeon) AS timed_count
            FROM runs r
            WHERE r.kind = 'mplus' AND r.timed = 1
              AND r.level = (
                  SELECT MAX(m.level) FROM runs m
                  WHERE m.kind = 'mplus' AND m.timed = 1 AND m.dungeon IS r.dungeon
              )
            GROUP BY r.dungeon
            ORDER BY best_level DESC, best_time IS NULL, best_time ASC, dungeon ASC
        """
        with self._lock:
            cur = self._conn.execute(query)
            rows = cur.fetchall()
            entries: list[LeaderboardEntry] = []
            for row in rows:
                # Identifie le run précis qui détient le record (niveau record,
                # puis temps le plus court) pour pouvoir retrouver son roster.
                record = self._conn.execute(
                    """
                    SELECT report_code, fight_id FROM runs
                    WHERE kind = 'mplus' AND timed = 1
                      AND dungeon IS ? AND level = ?
                    ORDER BY keystone_time IS NULL, keystone_time ASC
                    LIMIT 1
                    """,
                    (row["dungeon"], row["best_level"]),
                ).fetchone()
                entries.append(
                    LeaderboardEntry(
                        dungeon=row["dungeon"] or "?",
                        best_level=row["best_level"],
                        best_time_ms=row["best_time"],
                        timed_count=row["timed_count"],
                        report_code=record["report_code"] if record else None,
                        fight_id=record["fight_id"] if record else None,
                    )
                )
            return entries

    def weekly_counts(self, weeks: int = 6) -> list[tuple[str, int]]:
        """Nombre de clés M+ par semaine ISO, des plus anciennes aux récentes.

        Retourne `weeks` entrées (les plus anciennes à zéro si pas de données),
        chacune étiquetée « S<numéro> » pour un mini-graphe de tendance.
        Repose sur `created_at` (ISO), date de publication du run.
        """
        today = datetime.date.today()
        # Lundi de la semaine courante, puis recule semaine par semaine.
        monday = today - datetime.timedelta(days=today.weekday())
        buckets: list[tuple[datetime.date, datetime.date]] = []
        for i in range(weeks - 1, -1, -1):
            start = monday - datetime.timedelta(weeks=i)
            buckets.append((start, start + datetime.timedelta(days=7)))

        with self._lock:
            result: list[tuple[str, int]] = []
            for start, end in buckets:
                cur = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM runs "
                    "WHERE kind = 'mplus' AND created_at >= ? AND created_at < ?",
                    (start.isoformat(), end.isoformat()),
                )
                n = cur.fetchone()["n"] or 0
                result.append((f"S{start.isocalendar().week:02d}", n))
            return result

    # -- Sauvegarde -----------------------------------------------------------

    def backup(self, dest_path: str) -> None:
        """Écrit une copie cohérente et compacte de la base vers `dest_path`.

        Utilise `VACUUM INTO` (atomique, sans verrou long) ; le fichier de
        destination ne doit pas déjà exister (l'appelant utilise un nom horodaté).
        """
        if os.path.dirname(dest_path):
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with self._lock:
            self._conn.execute("VACUUM INTO ?", (dest_path,))
