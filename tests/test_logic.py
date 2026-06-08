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


def test_affix_label_known_and_unknown():
    assert logic.affix_label(10) == "Fortifié"
    assert logic.affix_label(999999) == "Affixe #999999"


def test_empty_report_degrades_cleanly():
    assert logic.extract_keystone_runs({}) == []
    assert logic.extract_raid_encounters({}) == []
    assert logic.report_date({})  # ne lève pas
