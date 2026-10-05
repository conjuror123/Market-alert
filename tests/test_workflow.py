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
