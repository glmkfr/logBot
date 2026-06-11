"""Tests du stockage SQLite : anti-doublon et statistiques."""

import datetime
import os

from bot.db import Database


def _db(tmp_path):
    return Database(str(tmp_path / "test.db"))


def test_anti_doublon(tmp_path):
    db = _db(tmp_path)
    assert not db.run_exists("ABC", 1)
    ok = db.record_run(
        report_code="ABC", fight_id=1, kind="mplus", dungeon="Skyreach",
        level=18, timed=True, keystone_time=1500000, encounter_id=None,
        date="01/01", thread_id=111,
    )
    assert ok is True
    assert db.run_exists("ABC", 1)
    # Réinsertion du même (report_code, fight_id) => refusée.
    again = db.record_run(
        report_code="ABC", fight_id=1, kind="mplus", dungeon="Skyreach",
        level=18, timed=True, keystone_time=1500000, encounter_id=None,
        date="01/01", thread_id=222,
    )
    assert again is False
    db.close()


def test_stats(tmp_path):
    db = _db(tmp_path)
    db.record_run(report_code="A", fight_id=1, kind="mplus", dungeon="Skyreach",
                  level=20, timed=True, keystone_time=1, encounter_id=None,
                  date="01/01", thread_id=1)
    db.record_run(report_code="A", fight_id=2, kind="mplus", dungeon="Skyreach",
                  level=18, timed=False, keystone_time=1, encounter_id=None,
                  date="01/01", thread_id=2)
    db.record_run(report_code="A", fight_id=3, kind="mplus", dungeon="Pit of Saron",
                  level=16, timed=True, keystone_time=1, encounter_id=None,
                  date="01/01", thread_id=3)
    s = db.stats()
    assert s.total == 3
    assert s.timed == 2
    assert round(s.avg_level, 1) == 18.0
    assert s.by_dungeon["Skyreach"] == 2
    assert abs(s.timed_pct - 66.666) < 0.1
    db.close()


def test_thread_links_upsert(tmp_path):
    db = _db(tmp_path)
    assert db.get_thread_links(123) == {}
    db.set_thread_link(123, "route", "https://r1")
    db.set_thread_link(123, "vod", "https://v1")
    assert db.get_thread_links(123) == {"route": "https://r1", "vod": "https://v1"}
    # Upsert : remplace la même (thread, kind).
    db.set_thread_link(123, "route", "https://r2")
    assert db.get_thread_links(123)["route"] == "https://r2"
    db.close()


def test_raid_thread_dedup(tmp_path):
    db = _db(tmp_path)
    assert db.get_raid_thread("R", "Naxxramas") is None
    assert db.record_raid_thread("R", "Naxxramas", 555) is True
    assert db.get_raid_thread("R", "Naxxramas") == 555
    assert db.record_raid_thread("R", "Naxxramas", 666) is False
    db.close()


def _record(db, *, code, fight, dungeon, level, timed, time_ms=1000):
    db.record_run(
        report_code=code, fight_id=fight, kind="mplus", dungeon=dungeon,
        level=level, timed=timed, keystone_time=time_ms, encounter_id=None,
        date="01/01", thread_id=fight,
    )


def test_stats_best_key(tmp_path):
    db = _db(tmp_path)
    _record(db, code="A", fight=1, dungeon="Skyreach", level=20, timed=True)
    _record(db, code="A", fight=2, dungeon="Pit of Saron", level=24, timed=False)  # non timée
    _record(db, code="A", fight=3, dungeon="Skyreach", level=22, timed=True)
    s = db.stats()
    # La meilleure clé TIMÉE est +22 Skyreach (la +24 n'est pas timée).
    assert s.best_level == 22
    assert s.best_dungeon == "Skyreach"
    db.close()


def test_leaderboard(tmp_path):
    db = _db(tmp_path)
    # Skyreach : record +22 (deux temps au niveau record => on garde le plus court).
    _record(db, code="A", fight=1, dungeon="Skyreach", level=22, timed=True, time_ms=900_000)
    _record(db, code="A", fight=2, dungeon="Skyreach", level=22, timed=True, time_ms=800_000)
    _record(db, code="A", fight=3, dungeon="Skyreach", level=18, timed=True)
    # Pit of Saron : seule clé timée +16 ; une +25 non timée ne compte pas.
    _record(db, code="B", fight=1, dungeon="Pit of Saron", level=16, timed=True)
    _record(db, code="B", fight=2, dungeon="Pit of Saron", level=25, timed=False)

    board = db.leaderboard()
    by_dungeon = {e.dungeon: e for e in board}
    assert by_dungeon["Skyreach"].best_level == 22
    assert by_dungeon["Skyreach"].best_time_ms == 800_000  # le plus court au niveau record
    assert by_dungeon["Skyreach"].timed_count == 3
    assert by_dungeon["Pit of Saron"].best_level == 16
    # Tri : Skyreach (+22) avant Pit of Saron (+16).
    assert board[0].dungeon == "Skyreach"
    # Le run record pointé est bien celui du temps le plus court (+22, fight 2).
    assert by_dungeon["Skyreach"].report_code == "A"
    assert by_dungeon["Skyreach"].fight_id == 2
    db.close()


def test_run_players_roster(tmp_path):
    db = _db(tmp_path)
    assert db.get_run_players("A", 1) == []
    db.record_run_players("A", 1, [("Alice-Hyjal", "Mage"), ("Bob", "Priest")])
    # Idempotent : un ré-enregistrement (doublon) ne crée pas de lignes en plus.
    db.record_run_players("A", 1, [("Alice-Hyjal", "Mage")])
    roster = db.get_run_players("A", 1)
    assert roster == [("Alice-Hyjal", "Mage"), ("Bob", "Priest")]
    db.close()


def test_runs_without_roster(tmp_path):
    db = _db(tmp_path)
    _record(db, code="A", fight=1, dungeon="Skyreach", level=20, timed=True)
    _record(db, code="A", fight=2, dungeon="Skyreach", level=18, timed=True)
    # Au départ, aucun roster : les deux runs sont à backfiller.
    assert db.runs_without_roster() == [("A", 1), ("A", 2)]
    # Une fois le roster du fight 1 renseigné, seul le fight 2 reste.
    db.record_run_players("A", 1, [("Alice", "Mage")])
    assert db.runs_without_roster() == [("A", 2)]
    db.close()


def test_player_run_rows(tmp_path):
    db = _db(tmp_path)
    _record(db, code="A", fight=1, dungeon="Skyreach", level=20, timed=True, time_ms=800_000)
    _record(db, code="A", fight=2, dungeon="Pit of Saron", level=16, timed=False)
    db.record_run_players("A", 1, [("Alice", "Mage"), ("Bob", "Priest")])
    # Le fight 2 n'a pas de roster : il ne produit aucune ligne joueur.
    rows = db.player_run_rows()
    assert len(rows) == 2
    assert sorted(r["character_name"] for r in rows) == ["Alice", "Bob"]
    row = next(r for r in rows if r["character_name"] == "Alice")
    assert row["dungeon"] == "Skyreach"
    assert row["level"] == 20
    assert row["timed"] == 1
    assert row["keystone_time"] == 800_000
    db.close()


def test_leaderboard_season_window(tmp_path):
    db = _db(tmp_path)
    _record(db, code="A", fight=1, dungeon="Skyreach", level=20, timed=True)
    # Fenêtre future : la clé (créée maintenant) en est exclue.
    assert db.leaderboard(since_iso="2999-01-01") == []
    # Fenêtre large : la clé est présente.
    board = db.leaderboard(since_iso="2000-01-01", until_iso="2999-01-01")
    assert board and board[0].dungeon == "Skyreach"
    db.close()


def test_player_run_rows_window(tmp_path):
    db = _db(tmp_path)
    _record(db, code="A", fight=1, dungeon="Skyreach", level=20, timed=True)
    db.record_run_players("A", 1, [("Alice", "Mage")])
    assert db.player_run_rows(since_iso="2999-01-01") == []
    assert len(db.player_run_rows(since_iso="2000-01-01", until_iso="2999-01-01")) == 1
    db.close()


def test_dungeon_record(tmp_path):
    db = _db(tmp_path)
    assert db.dungeon_record("Skyreach") is None
    _record(db, code="A", fight=1, dungeon="Skyreach", level=18, timed=True, time_ms=900_000)
    _record(db, code="A", fight=2, dungeon="Skyreach", level=20, timed=True, time_ms=800_000)
    _record(db, code="A", fight=3, dungeon="Skyreach", level=20, timed=True, time_ms=700_000)
    _record(db, code="A", fight=4, dungeon="Skyreach", level=24, timed=False)  # non timé
    # Niveau record 20, meilleur temps 700k (la +24 non timée ne compte pas).
    assert db.dungeon_record("Skyreach") == (20, 700_000)
    # Hors fenêtre temporelle -> aucun record.
    assert db.dungeon_record("Skyreach", since_iso="2999-01-01") is None
    db.close()


def test_seasons_crud(tmp_path):
    db = _db(tmp_path)
    assert db.list_seasons() == []
    s1 = db.add_season("S1", "2026-01-01")
    db.add_season("S2", "2026-06-01")
    # Tri par date de début croissante.
    assert [s.name for s in db.list_seasons()] == ["S1", "S2"]
    # Date de début dupliquée refusée.
    assert db.add_season("dup", "2026-01-01") is None
    # Recherche par nom insensible à la casse.
    assert db.get_season_by_name("s2").start_date == "2026-06-01"
    assert db.get_season_by_name("inconnue") is None
    # Suppression.
    assert db.delete_season(s1.id) is True
    assert db.delete_season(999_999) is False
    assert [s.name for s in db.list_seasons()] == ["S2"]
    db.close()


def test_member_links(tmp_path):
    db = _db(tmp_path)
    assert db.get_character_link("alice") is None
    db.link_character("alice", 111)
    db.link_character("bob", 111)
    assert db.get_character_link("alice") == 111
    assert sorted(db.get_links_for_user(111)) == ["alice", "bob"]
    assert db.all_character_links() == {"alice": 111, "bob": 111}
    # Upsert : ré-attribution à un autre membre.
    db.link_character("alice", 222)
    assert db.get_character_link("alice") == 222
    # Suppression réservée au propriétaire.
    assert db.unlink_character("alice", 111) is False  # pas le bon membre
    assert db.unlink_character("alice", 222) is True
    assert db.get_character_link("alice") is None
    db.close()


def test_weekly_counts_length_and_recent(tmp_path):
    db = _db(tmp_path)
    _record(db, code="A", fight=1, dungeon="Skyreach", level=20, timed=True)
    trend = db.weekly_counts(weeks=6)
    assert len(trend) == 6
    # Le run vient d'être inséré (created_at = maintenant) => compté dans la dernière semaine.
    assert trend[-1][1] == 1
    assert sum(n for _, n in trend) == 1
    db.close()


def test_backup_creates_readable_copy(tmp_path):
    db = _db(tmp_path)
    _record(db, code="A", fight=1, dungeon="Skyreach", level=20, timed=True)
    dest = str(tmp_path / "backups" / "copy.db")
    db.backup(dest)
    assert os.path.exists(dest)
    # La copie est une base SQLite valide contenant le run.
    copy = Database(dest)
    assert copy.stats().total == 1
    copy.close()
    db.close()
