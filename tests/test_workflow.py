"""The hourly workflow's step conditions (.github/workflows/price-monitor.yml)."""
import os

import yaml

WORKFLOW = os.path.join(os.path.dirname(__file__), "..", ".github", "workflows",
                        "price-monitor.yml")


def _steps():
    with open(WORKFLOW, encoding="utf-8") as f:
        job = yaml.safe_load(f)["jobs"]["monitor"]
    return {step.get("name"): step for step in job["steps"]}


def test_the_state_is_committed_even_when_the_monitor_step_fails():
    # A run with an error makes `python -m price_monitor` exit 1, and that run's
    # state.json - the health streak, what it sent - must still reach git, or
    # the streak never grows and the next run sends the same messages again.
    # GitHub skips a step after a failed one unless its condition says always().
    steps = _steps()
    monitor = steps["Run monitor"]
    commit = steps["Commit updated state"]
    condition = str(commit.get("if", ""))
    assert "always()" in condition
    assert monitor.get("id") and f"steps.{monitor['id']}.outcome" in condition


def _job():
    with open(WORKFLOW, encoding="utf-8") as f:
        return yaml.safe_load(f)["jobs"]["monitor"]


def test_the_fetch_and_score_step_runs_out_of_time_before_the_job_does():
    # The job's own timeout cancels every step after it: no delivery, no health
    # line, no commit (28 runs on 2026-09-09..11). The step's limit leaves the
    # rest of the job its turn.
    job, steps = _job(), _steps()
    jump = steps["Jump pipeline"]
    assert jump.get("timeout-minutes")
    assert job["timeout-minutes"] - jump["timeout-minutes"] >= 6


def test_delivery_runs_after_a_fetch_and_score_step_that_ran_out_of_time():
    # A step that timed out may end the job's success(); delivery and health
    # must still run, unless the step never started (setup failed before it).
    steps = _steps()
    jump_id = steps["Jump pipeline"]["id"]
    condition = str(steps["Run monitor"].get("if", ""))
    assert "!cancelled()" in condition
    assert f"steps.{jump_id}.outcome != 'skipped'" in condition
    env = steps["Run monitor"]["env"]
    # Not only 'failure': a step stopped for time must count as not completed.
    assert f"steps.{jump_id}.outcome != 'success'" in env["JUMP_PIPELINE_CRASHED"]
    assert "JUMP_STAGE_FILE" in env


def test_the_monitor_hears_how_the_side_steps_went():
    steps = _steps()
    env = steps["Run monitor"]["env"]
    sessions = steps["Extend the session table when it runs short"]
    save = steps["Save the open months of the bars"]
    assert env["SESSIONS_STEP_OUTCOME"] == "${{ steps.%s.outcome }}" % sessions["id"]
    assert env["HOT_SAVE_STEP_OUTCOME"] == "${{ steps.%s.outcome }}" % save["id"]


def test_a_repair_is_asked_for_by_hand_and_reaches_the_step_as_data():
    with open(WORKFLOW, encoding="utf-8") as f:
        workflow = yaml.safe_load(f)
    repair = workflow[True]["workflow_dispatch"]["inputs"]["repair"]
    assert repair["required"] is False and repair["default"] == ""
    step = _steps()["Jump pipeline"]
    # Through the environment, never pasted into the script: an input is text
    # anyone who can dispatch the workflow chooses.
    assert step["env"]["REPAIR"] == "${{ github.event.inputs.repair }}"
    assert "inputs.repair" not in step["run"]
    assert 'python -m jump.backfill --repair --instruments "$REPAIR"' in step["run"]
    assert step["run"].index("--repair") < step["run"].index("stage fetch")


def test_a_rejected_state_push_is_retried_beside_the_uncommitted_vix_file(tmp_path):
    # 2026-10-07 15:05: GitHub answered the push with a 500, and the retry's
    # `git pull --rebase` refused to run - the VIX file is rewritten most runs
    # but committed only on Saturdays. The state was lost. The step's own
    # retry loop, run here against a branch that moved meanwhile.
    import re
    import subprocess

    script = _steps()["Commit updated state"]["run"]
    loop = script[script.index("for attempt in"):script.index("done", script.index("for attempt in")) + 4]
    loop = re.sub(r"\$\{\{ github\.ref_name \}\}", "main", loop).replace("sleep $((attempt * 3))", "true")

    def git(*args, cwd):
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd,
                       check=True, capture_output=True)

    origin, mine, theirs = tmp_path / "origin.git", tmp_path / "mine", tmp_path / "theirs"
    git("init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    git("clone", "-q", str(origin), str(mine), cwd=tmp_path)
    (mine / "vix").write_text("v1")
    (mine / "state").write_text("s1")
    git("add", ".", cwd=mine)
    git("commit", "-q", "-m", "base", cwd=mine)
    git("push", "-q", "origin", "HEAD:main", cwd=mine)
    git("clone", "-q", str(origin), str(theirs), cwd=tmp_path)
    (theirs / "other").write_text("moved")
    git("add", "other", cwd=theirs)
    git("commit", "-q", "-m", "the branch moved", cwd=theirs)
    git("push", "-q", "origin", "HEAD:main", cwd=theirs)

    (mine / "vix").write_text("v2")                  # rewritten, not committed
    (mine / "state").write_text("s2")
    git("add", "state", cwd=mine)
    git("commit", "-q", "-m", "the run's state", cwd=mine)
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    done = subprocess.run(["bash", "-c", loop], cwd=mine, env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr
    log = subprocess.run(["git", "log", "--format=%s", "origin/main"], cwd=mine,
                         capture_output=True, text=True).stdout
    assert "the run's state" in log and "the branch moved" in log
    assert (mine / "vix").read_text() == "v2"          # still uncommitted, as before
