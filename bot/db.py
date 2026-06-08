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
"""


@dataclass
class Stats:
    total: int
    timed: int
    avg_level: float
    by_dungeon: dict[str, int]

    @property
    def timed_pct(self) -> float:
        return (self.timed / self.total * 100.0) if self.total else 0.0


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

        return Stats(
            total=total, timed=timed, avg_level=avg_level, by_dungeon=by_dungeon
        )
