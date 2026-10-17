"""tools/run_died.sh: the health line for a run whose state was not committed,
sent from outside Python. Run in a scratch repository, against a stand-in for
curl."""
import json
import os
import re
import stat
import subprocess
import time

import pytest
import yaml

ROOT = os.path.join(os.path.dirname(__file__), "..")
SCRIPT = os.path.join(ROOT, "tools", "run_died.sh")
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "price-monitor.yml")

CURL = """#!/usr/bin/env bash
printf '%s\\n' "$@" >> "$CURL_LOG"
echo 200
"""


def _steps(**outcomes):
    return json.dumps({k: {"outcome": v, "conclusion": v, "outputs": {}}
                       for k, v in outcomes.items()})


DEAD = _steps(checkout="success", hot="failure", jump="skipped", monitor="skipped",
              commit="skipped")


@pytest.fixture
def run(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(CURL)
    curl.chmod(curl.stat().st_mode | stat.S_IEXEC)
    repo, log = tmp_path / "repo", tmp_path / "curl.log"
    repo.mkdir()

    def git(*args):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                              cwd=repo, check=True, capture_output=True, text=True).stdout

    def go(steps, hours_ago=None):
        """`hours_ago`: when the last committed run was; None for no record."""
        git("init", "-q") if not (repo / ".git").exists() else None
        state = {} if hours_ago is None else {
            "_monitoring_health": {"consecutive_failures": 0,
                                   "last_run_utc": int(time.time() - hours_ago * 3600)}}
        (repo / "data").mkdir(exist_ok=True)
        (repo / "data" / "state.json").write_text(json.dumps(state))
        git("add", "-A")
        git("commit", "-q", "--allow-empty", "-m", "state")
        sha = git("rev-parse", "HEAD").strip()
        # After a failed commit the file on disk is this run's own: never read.
        (repo / "data" / "state.json").write_text(json.dumps(
            {"_monitoring_health": {"last_run_utc": int(time.time())}}))
        env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", STEPS=steps,
                   TELEGRAM_BOT_TOKEN="123:SECRET", TELEGRAM_HEALTH_CHAT_ID="ops",
                   RUN_URL="https://github.com/o/r/actions/runs/9", GITHUB_SHA=sha,
                   CURL_LOG=str(log))
        out = subprocess.run(["bash", SCRIPT], cwd=repo, env=env, capture_output=True,
                             text=True)
        sent = [line[len("text="):] for line in log.read_text().splitlines()
                if line.startswith("text=")] if log.exists() else []
        log.unlink(missing_ok=True)
        return out, sent
    return go


def test_a_run_stopped_before_the_monitor_names_the_step(run):
    out, sent = run(DEAD, hours_ago=1)
    assert out.returncode == 0
    assert len(sent) == 1
    assert 'stopped at "Restore the open months of the bars"' in sent[0]
    assert "SECRET" not in out.stdout + out.stderr


def test_a_step_allowed_to_fail_is_not_blamed(run):
    _, sent = run(_steps(sessions="failure", hot="failure", monitor="skipped",
                         commit="skipped"), hours_ago=1)
    assert 'stopped at "Restore the open months of the bars"' in sent[0]


@pytest.mark.parametrize("commit", ["failure", "cancelled"])
def test_a_state_that_was_not_committed_is_reported(run, commit):
    _, sent = run(_steps(monitor="success", commit=commit), hours_ago=1)
    assert len(sent) == 1 and "could not commit its state" in sent[0]


def test_a_committed_run_sends_nothing(run):
    _, sent = run(_steps(monitor="failure", commit="success"), hours_ago=1)
    assert sent == []


def test_an_outage_is_reported_at_its_first_hour_then_daily(run):
    # Timed by the last committed run, which no page of run history can cap:
    # counted from the newest 25 runs, the 25th dead run and every one after
    # it reported - hourly from the second day.
    sent = {h: run(DEAD, hours_ago=h)[1] for h in (1, 2, 24, 25, 26, 49, 73)}
    assert {h for h, s in sent.items() if s} == {1, 25, 49, 73}
    assert "(25 h since the last run that delivered.)" in sent[25][0]


def test_with_no_record_it_reports(run):
    _, sent = run(DEAD, hours_ago=None)
    assert len(sent) == 1


def test_the_script_and_the_workflow_agree():
    with open(WORKFLOW, encoding="utf-8") as f:
        steps = yaml.safe_load(f)["jobs"]["monitor"]["steps"]
    with open(SCRIPT, encoding="utf-8") as f:
        script = f.read()
    named = dict(re.findall(r"^    (\w+)\) echo \"([^\"]+)\" ;;$", script, re.M))
    by_id = {s["id"]: s["name"] for s in steps if "id" in s}
    # Every step before the monitor can stop the run; each must be named.
    before = steps[:[s.get("id") for s in steps].index("monitor")]
    assert all("id" in s for s in before), [s["name"] for s in before if "id" not in s]
    for step_id, name in named.items():
        assert by_id[step_id] == name
    assert {s["id"] for s in before} <= set(named)
    last = steps[-1]
    assert "always()" in last["if"] and "steps.commit.outcome != 'success'" in last["if"]
