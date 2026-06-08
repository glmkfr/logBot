"""Tests du parsing d'URL et de la génération de liens profonds."""

from bot import links


def test_extract_report_code_standard():
    url = "https://www.warcraftlogs.com/reports/aBcD1234xyZ#fight=3"
    assert links.extract_report_code(url) == "aBcD1234xyZ"


def test_extract_report_code_no_www():
    url = "https://warcraftlogs.com/reports/Kk99Zz"
    assert links.extract_report_code(url) == "Kk99Zz"


def test_extract_report_code_invalid():
    assert links.extract_report_code("https://example.com/foo") is None
    assert links.extract_report_code("") is None


def test_is_warcraftlogs_url_accepts_official():
    assert links.is_warcraftlogs_url("https://www.warcraftlogs.com/reports/abc")
    assert links.is_warcraftlogs_url("https://warcraftlogs.com/reports/abc")
    # Tolère l'absence de schéma.
    assert links.is_warcraftlogs_url("warcraftlogs.com/reports/abc")


def test_is_warcraftlogs_url_accepts_locale_subdomains():
    # Sous-domaines de langue (fr., de., ko., …) acceptés.
    assert links.is_warcraftlogs_url("https://fr.warcraftlogs.com/reports/abc#fight=2")
    assert links.is_warcraftlogs_url("https://de.warcraftlogs.com/reports/abc")
    assert links.is_warcraftlogs_url("https://ko.warcraftlogs.com/reports/abc")


def test_is_warcraftlogs_url_rejects_others():
    assert not links.is_warcraftlogs_url("https://evil.com/reports/abc")
    assert not links.is_warcraftlogs_url("https://fakewarcraftlogs.com/reports/abc")
    assert not links.is_warcraftlogs_url("https://warcraftlogs.com.evil.com/x")
    assert not links.is_warcraftlogs_url("https://warcraftlogs.evil.com/x")
    assert not links.is_warcraftlogs_url("")


def test_wcl_fight_url():
    assert (
        links.wcl_fight_url("abc", 7)
        == "https://www.warcraftlogs.com/reports/abc#fight=7"
    )
    assert links.wcl_fight_url("abc", None) == "https://www.warcraftlogs.com/reports/abc"


def test_wowanalyzer_url():
    assert links.wowanalyzer_url("abc", 7) == "https://wowanalyzer.com/report/abc/7"
    assert links.wowanalyzer_url("abc") == "https://wowanalyzer.com/report/abc"
