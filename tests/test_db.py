"""Tests du stockage SQLite : anti-doublon et statistiques."""

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
