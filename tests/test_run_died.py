"""tools/run_died.sh: the health line for a run that could not deliver, sent
from outside Python. Run against stand-ins for gh and curl."""
import json
import os
import re
import stat
import subprocess

import pytest
import yaml

ROOT = os.path.join(os.path.dirname(__file__), "..")
SCRIPT = os.path.join(ROOT, "tools", "run_died.sh")
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "price-monitor.yml")

GH = """#!/usr/bin/env bash
[ -n "${FAKE_GH_FAIL:-}" ] && exit 1
case "$2" in
  */workflows/*) for id in $FAKE_RUNS; do echo "$id"; done ;;
  */jobs) id=$(sed -E 's#.*/runs/([0-9]+)/jobs#\\1#' <<<"$2")
          if [[ " $FAKE_REPORTED " == *" $id "* ]]; then echo success; else echo skipped; fi ;;
esac
"""
CURL = """#!/usr/bin/env bash
printf '%s\\n' "$@" >> "$CURL_LOG"
echo 200
"""


def _steps(**outcomes):
    return json.dumps({k: {"outcome": v, "conclusion": v, "outputs": {}}
                       for k, v in outcomes.items()})


@pytest.fixture
def run(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("gh", GH), ("curl", CURL)):
        path = bin_dir / name
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "curl.log"

    def go(steps, runs=(), reported=(), gh_fails=False):
        env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", STEPS=steps,
                   TELEGRAM_BOT_TOKEN="123:SECRET", TELEGRAM_HEALTH_CHAT_ID="ops",
                   GH_TOKEN="x", RUN_URL="https://github.com/o/r/actions/runs/9",
                   GITHUB_REPOSITORY="o/r", GITHUB_REF_NAME="live", GITHUB_RUN_ID="9",
                   CURL_LOG=str(log), FAKE_RUNS=" ".join(map(str, runs)),
                   FAKE_REPORTED=" ".join(map(str, reported)))
        if gh_fails:
            env["FAKE_GH_FAIL"] = "1"
        out = subprocess.run(["bash", SCRIPT], env=env, capture_output=True, text=True)
        sent = [line[len("text="):] for line in log.read_text().splitlines()
                if line.startswith("text=")] if log.exists() else []
        log.unlink(missing_ok=True)
        return out, sent
    return go


def test_a_run_stopped_before_the_monitor_names_the_step(run):
    out, sent = run(_steps(checkout="success", hot="failure", tremor="skipped",
                           monitor="skipped", commit="skipped"))
    assert out.returncode == 0
    assert len(sent) == 1
    assert 'stopped at "Restore the open months of the bars"' in sent[0]
    assert "SECRET" not in out.stdout + out.stderr


def test_a_step_allowed_to_fail_is_not_blamed(run):
    out, sent = run(_steps(sessions="failure", hot="failure", monitor="skipped"))
    assert 'stopped at "Restore the open months of the bars"' in sent[0]


def test_a_state_that_was_not_committed_is_reported(run):
    _, sent = run(_steps(monitor="success", commit="failure"))
    assert len(sent) == 1 and "could not commit its state" in sent[0]


def test_a_run_that_delivered_and_committed_sends_nothing(run):
    _, sent = run(_steps(monitor="failure", commit="success"))
    assert sent == []


def test_an_outage_is_reported_once_then_daily(run):
    steps = _steps(hot="failure", monitor="skipped")
    # The run before this one reported: the second in a row is not reported.
    assert run(steps, runs=[8, 7], reported=[8])[1] == []
    # The 25th in a row is.
    earlier = list(range(100, 76, -1))
    _, sent = run(steps, runs=earlier + [50], reported=earlier)
    assert len(sent) == 1 and "(25 runs in a row.)" in sent[0]


def test_when_the_history_cannot_be_read_it_reports(run):
    _, sent = run(_steps(hot="failure", monitor="skipped"), gh_fails=True)
    assert len(sent) == 1


def test_the_script_and_the_workflow_agree():
    with open(WORKFLOW, encoding="utf-8") as f:
        workflow = yaml.safe_load(f)
    steps = workflow["jobs"]["monitor"]["steps"]
    with open(SCRIPT, encoding="utf-8") as f:
        script = f.read()
    report = re.search(r'REPORT_STEP="([^"]+)"', script).group(1)
    named = dict(re.findall(r"^    (\w+)\) echo \"([^\"]+)\" ;;$", script, re.M))
    by_id = {s["id"]: s["name"] for s in steps if "id" in s}
    # Every step before the monitor can stop the run; each must be named.
    before = [s for s in steps[:[s.get("id") for s in steps].index("monitor")]]
    assert all("id" in s for s in before), [s["name"] for s in before if "id" not in s]
    for step_id, name in named.items():
        assert by_id[step_id] == name
    assert set(s["id"] for s in before) <= set(named)
    last = steps[-1]
    assert last["name"] == report
    assert "always()" in last["if"]
    assert "steps.monitor.outcome" in last["if"] and "steps.commit.outcome" in last["if"]
    assert workflow["permissions"].get("actions") == "read"
