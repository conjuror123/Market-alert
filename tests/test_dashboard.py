"""tools/dashboard.py: the page from one measurement, and nothing kept between runs."""
import json
import os

from tools import dashboard


def _result():
    return {"meta": {"commit": "abc1234", "subject": "a change", "measured_at": "2026-10-12 09:00 UTC",
                     "instruments": 58, "levels": [4.5, 6.4, 9.0, 12.7]},
            "windows": [{"name": "year to 2026-10-12 09:00"}], "votes": None}


def _payload(page):
    return json.loads(page.split('<script id="payload" type="application/json">')[1]
                      .split("</script>")[0])


def test_the_page_carries_the_numbers_and_cannot_be_closed_early_by_them():
    result = _result()
    result["meta"]["subject"] = "</script><b>x</b>"
    page = dashboard.render(result)
    assert "__PAYLOAD__" not in page
    assert _payload(page) == result


def test_a_run_writes_only_its_own_page_and_measurement(tmp_path):
    measured = tmp_path / "m.json"
    measured.write_text(json.dumps(_result()))
    site = tmp_path / "site"
    assert dashboard.main([str(measured), str(site)]) == 0
    assert sorted(os.listdir(site)) == ["index.html", "latest.json"]
    assert json.loads((site / "latest.json").read_text()) == _result()
    assert _payload((site / "index.html").read_text()) == _result()


def test_the_page_has_a_place_for_the_provider_limits():
    page = dashboard.render(_result())
    assert '<table id="t-limits">' in page and "M.limits" in page


def _script(page):
    return page.split('<script id="payload" type="application/json">')[1] \
        .split("</script>")[1].split("<script>")[1]


def test_the_pages_script_parses():
    # One name declared twice at the top level stops the whole script, and the
    # page draws nothing: the limits' `pct` beside the votes' (2026-10-12..17).
    import re
    import shutil
    import subprocess
    script = _script(dashboard.render(_result()))
    names = re.findall(r"^(?:const|let|var|function)\s+([A-Za-z_$][\w$]*)", script, re.M)
    assert sorted({n for n in names if names.count(n) > 1}) == []
    node = shutil.which("node")
    if node:
        out = subprocess.run([node, "--check", "-"], input=script, capture_output=True,
                             text=True, timeout=30)
        assert out.returncode == 0, out.stderr
