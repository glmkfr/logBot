"""Tests de la couche métier : extraction M+/raid, titres, formatage."""

from bot import logic


def _report():
    """Un rapport fictif mêlant M+ et raid, calqué sur le schéma WCL v2."""
    return {
        "code": "RpT123",
        "startTime": 1_700_000_000_000,  # ms
        "title": "Soirée clés",
        "zone": {"id": 42, "name": "Pit of Saron"},
        "masterData": {
            "actors": [
                {"id": 1, "name": "Aaa", "subType": "Mage"},
                {"id": 2, "name": "Bbb", "subType": "Mage"},
                {"id": 3, "name": "Ccc", "subType": "Priest"},
            ]
        },
        "fights": [
            {
                "id": 1,
                "name": "Skyreach",
                "encounterID": 0,
                "kill": None,
                "keystoneLevel": 18,
                "keystoneBonus": 2,
                "keystoneTime": 1_500_000,  # 25:00
                "keystoneAffixes": [10, 124],
                "gameZone": {"id": 1, "name": "Skyreach"},
                "friendlyPlayers": [1, 2, 3],
                "averageItemLevel": 489.6,
                "startTime": 0,
            },
            {
                "id": 2,
                "name": "Skyreach",
                "encounterID": 0,
                "kill": None,
                "keystoneLevel": 18,
                "keystoneBonus": 0,
                "keystoneTime": 2_000_000,
                "keystoneAffixes": [10, 124],
                "gameZone": {"id": 1, "name": "Skyreach"},
                "friendlyPlayers": [1, 3],
                "startTime": 3_600_000,  # +1h
            },
            # Boss de raid : deux essais, pas de kill.
            {
                "id": 3,
                "name": "Lich King",
                "encounterID": 999,
                "kill": False,
                "fightPercentage": 35.0,
                "startTime": 7_200_000,
            },
            {
                "id": 4,
                "name": "Lich King",
                "encounterID": 999,
                "kill": True,
                "fightPercentage": 0.0,
                "startTime": 7_900_000,
            },
        ],
    }


def test_extract_keystone_runs_count_and_fields():
    runs = logic.extract_keystone_runs(_report())
    assert len(runs) == 2
    r = runs[0]
    assert r.report_code == "RpT123"
    assert r.level == 18
    assert r.timed is True  # bonus=2 > 0
    assert r.dungeon == "Skyreach"
    assert r.dungeon_abbr == "SKY"
    assert r.affixes == [10, 124]
    assert r.item_level == 489.6


def test_item_level_absent_is_none():
    report = _report()
    del report["fights"][0]["averageItemLevel"]
    runs = logic.extract_keystone_runs(report)
    assert runs[0].item_level is None


def test_extract_keystone_runs_untimed():
    runs = logic.extract_keystone_runs(_report())
    assert runs[1].timed is False  # bonus=0


def test_build_titles_disambiguates_same_dungeon_same_day():
    runs = logic.extract_keystone_runs(_report())
    titles = logic.build_titles(runs)
    # Deux SKY +18 le même jour => titres distincts (heure ajoutée).
    assert titles[1] != titles[2]
    assert all(t.startswith("SKY +18") for t in titles.values())


def test_extract_raid_picks_kill_as_representative():
    encounters = logic.extract_raid_encounters(_report())
    assert len(encounters) == 1
    enc = encounters[0]
    assert enc.encounter_id == 999
    assert enc.killed is True
    assert enc.fight_id == 4  # le kill, pas le wipe
    assert enc.pulls == 2


def test_extract_raid_best_try_when_no_kill():
    report = _report()
    # Retire le kill : il ne reste que le wipe à 35 %.
    report["fights"] = [f for f in report["fights"] if f["id"] != 4]
    encounters = logic.extract_raid_encounters(report)
    assert len(encounters) == 1
    assert encounters[0].killed is False
    assert encounters[0].fight_id == 3
    assert encounters[0].best_percentage == 35.0


def test_format_duration():
    assert logic.format_duration(1_500_000) == "25:00"
    assert logic.format_duration(None) is None
    assert logic.format_duration(0) is None
    assert logic.format_duration(3_661_000) == "1:01:01"


def test_composition_summary_counts_classes():
    report = _report()
    fight = report["fights"][0]
    summary = logic.composition_summary(report, fight)
    assert "2x Mage" in summary
    assert "Priest" in summary


def test_composition_names_returns_characters():
    report = _report()
    fight = report["fights"][0]
    names = logic.composition_names(report, fight)
    assert names == [("Aaa", "Mage"), ("Bbb", "Mage"), ("Ccc", "Priest")]


def test_composition_names_empty_degrades_cleanly():
    assert logic.composition_names({}, {}) == []


def test_normalize_character_strips_realm_and_accents():
    assert logic.normalize_character("Bûcheronx-Hyjal") == "bucheronx"
    assert logic.normalize_character("  Aaa  ") == "aaa"
    assert logic.normalize_character("") == ""


def _prow(rc, fid, name, dungeon, level, timed, time_ms):
    return {
        "report_code": rc, "fight_id": fid, "character_name": name,
        "dungeon": dungeon, "level": level, "timed": timed, "keystone_time": time_ms,
    }


def test_player_rankings_orders_and_filters():
    rows = [
        _prow("A", 1, "Alice", "Skyreach", 20, 1, 800_000),
        _prow("A", 2, "Alice", "Pit of Saron", 18, 1, 600_000),
        _prow("B", 1, "Bob", "Pit of Saron", 18, 1, 700_000),
        _prow("C", 1, "Alice", "Skyreach", 24, 0, None),   # non timé : ignoré
        _prow("D", 1, "Carol", "Skyreach", 16, 1, 500_000),  # non résolu : exclu
    ]
    resolver = {"alice": 111, "bob": 222}  # Carol absente du resolver
    ranking = logic.player_rankings(rows, resolver)

    assert [r.user_id for r in ranking] == [111, 222]  # Alice (+20) avant Bob (+18)
    alice = ranking[0]
    assert alice.best_level == 20  # la +24 non timée ne compte pas
    assert alice.best_dungeon == "Skyreach"
    assert alice.timed_count == 2
    assert alice.avg_level == 19.0  # (20 + 18) / 2


def test_player_profile_stats_and_partners():
    rows = [
        _prow("A", 1, "Alice", "Skyreach", 20, 1, 800_000),
        _prow("A", 1, "Bob", "Skyreach", 20, 1, 800_000),
        _prow("A", 2, "Alice", "Skyreach", 18, 1, 900_000),  # même donjon, moins bien
        _prow("B", 1, "Alice", "Pit of Saron", 16, 0, None),  # non timé
        _prow("B", 1, "Carol", "Pit of Saron", 16, 0, None),
    ]
    resolver = {"alice": 111, "bob": 222, "carol": 333}
    p = logic.player_profile(rows, resolver, 111)

    assert p.total == 3                      # runs A/1, A/2, B/1
    assert p.timed == 2
    assert round(p.timed_pct) == 67
    # Meilleure clé timée par donjon : Skyreach +20 ; Pit (non timé) absent.
    assert p.best_by_dungeon[0] == ("Skyreach", 20, 800_000)
    assert all(d != "Pit of Saron" for d, _, _ in p.best_by_dungeon)
    # Partenaires : Bob (run A/1) et Carol (run B/1), une fois chacun.
    assert dict(p.partners) == {222: 1, 333: 1}


def test_current_season_and_bounds():
    from bot.db import Season
    seasons = [
        Season(1, "S1", "2026-01-01"),
        Season(2, "S2", "2026-06-01"),
        Season(3, "S3", "2026-09-01"),
    ]
    # current_season : dernière saison débutée au plus tard à la date donnée.
    assert logic.current_season(seasons, "2026-07-15").name == "S2"
    assert logic.current_season(seasons, "2026-09-01").name == "S3"  # borne incluse
    assert logic.current_season(seasons, "2025-12-31") is None
    # bounds : du début de la saison au début de la suivante (None si dernière).
    assert logic.season_bounds(seasons, seasons[1]) == ("2026-06-01", "2026-09-01")
    assert logic.season_bounds(seasons, seasons[2]) == ("2026-09-01", None)


def test_runs_together_counts_shared_runs():
    rows = [
        _prow("A", 1, "Alice", "Skyreach", 20, 1, 800_000),
        _prow("A", 1, "Bob", "Skyreach", 20, 1, 800_000),     # ensemble (run A/1)
        _prow("B", 1, "Alice", "Pit of Saron", 18, 1, 700_000),
        _prow("B", 1, "Carol", "Pit of Saron", 18, 1, 700_000),  # Alice+Carol
        _prow("C", 1, "Bob", "Skyreach", 16, 1, 500_000),     # Bob seul
    ]
    resolver = {"alice": 111, "bob": 222, "carol": 333}
    assert logic.runs_together(rows, resolver, 111, 222) == 1  # Alice & Bob : A/1
    assert logic.runs_together(rows, resolver, 111, 333) == 1  # Alice & Carol : B/1
    assert logic.runs_together(rows, resolver, 222, 333) == 0  # jamais ensemble


def test_best_key_returns_top_or_none():
    rows = [
        _prow("A", 1, "Alice", "Skyreach", 20, 1, 800_000),
        _prow("A", 2, "Alice", "Pit of Saron", 18, 1, 600_000),
    ]
    resolver = {"alice": 111}
    p = logic.player_profile(rows, resolver, 111)
    assert logic.best_key(p) == ("Skyreach", 20, 800_000)
    empty = logic.player_profile(rows, {"bob": 222}, 222)
    assert logic.best_key(empty) is None


def test_beats_record():
    assert logic.beats_record(None, 18, 1000) is True          # premier record
    assert logic.beats_record((20, 800), 22, 900) is True       # niveau supérieur
    assert logic.beats_record((20, 800), 18, 100) is False      # niveau inférieur
    assert logic.beats_record((20, 800), 20, 700) is True       # = niveau, plus rapide
    assert logic.beats_record((20, 800), 20, 900) is False      # = niveau, plus lent
    assert logic.beats_record((20, 800), 20, 800) is False      # = niveau, = temps
    assert logic.beats_record((20, None), 20, 700) is True      # record précédent sans temps
    assert logic.beats_record((20, 800), 20, None) is False     # run sans temps connu


def test_affix_label_known_and_unknown():
    assert logic.affix_label(10) == "Fortifié"
    assert logic.affix_label(999999) == "Affixe #999999"


def test_empty_report_degrades_cleanly():
    assert logic.extract_keystone_runs({}) == []
    assert logic.extract_raid_encounters({}) == []
    assert logic.report_date({})  # ne lève pas
