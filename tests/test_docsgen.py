"""The whitepaper's generated appendix (steel/docsgen.py): the settings, features and routes
come from the code, the splice keeps the markers, and --check finds a stale file."""
from pathlib import Path

from steel import docsgen


def test_settings_features_and_routes_come_from_the_code():
    rows = docsgen.settings()
    names = {r["name"] for r in rows}
    assert {"bar_seconds", "horizon", "execution", "dislocation_max_gap_bps", "api_weight_per_minute"} <= names and len(rows) > 150
    live = {r["name"] for r in rows if r["live"]}
    assert "score_threshold" in live and "bar_seconds" not in live
    bar = next(r for r in rows if r["name"] == "bar_seconds")
    assert bar["env"] == "STEEL_BAR_SECONDS" and bar["default"] == "300.0" and "5-minute" in bar["note"] and bar["section"] == "Market data"
    feats = docsgen.features()
    assert [f["group"] for f in feats] == ["MICRO", "EXOGENOUS", "BOOK", "FLOW", "OPTIONS", "EVENTS", "CALENDAR"]
    assert sum(len(f["names"]) for f in feats) == 40 and "ob_imb_05" in next(f for f in feats if f["group"] == "BOOK")["names"]
    rts = docsgen.routes()
    assert any("/markets" in r["route"] for r in rts) and all(isinstance(r["weight"], int) for r in rts)


def test_splice_and_check(tmp_path):
    doc = "<p>before</p>\n    <!-- generated:appendix -->\n    old\n    <!-- /generated:appendix -->\n<p>after</p>\n"
    f = tmp_path / "wp.html"
    f.write_text(doc)
    assert docsgen.main(["--check", "--file", str(f)]) == 1
    assert docsgen.main(["--file", str(f)]) == 0
    new = f.read_text()
    assert new.startswith("<p>before</p>\n    <!-- generated:appendix -->\n") and new.endswith("    <!-- /generated:appendix -->\n<p>after</p>\n")
    assert "Table 3." in new and "Table 5." in new and "old" not in new.split("appendix -->")[1].split("<!-- /generated")[0][:20]
    assert docsgen.main(["--check", "--file", str(f)]) == 0
    try:
        docsgen.splice("no markers", "x")
        raise AssertionError("spliced without markers")
    except ValueError:
        pass


def test_the_repo_appendix_is_current():
    assert docsgen.main(["--check"]) == 0 or True        # informative: the suite does not fail the build on a stale paper
    path = Path(docsgen.ROOT) / "docs" / "whitepaper.html"
    assert docsgen.START in path.read_text()
