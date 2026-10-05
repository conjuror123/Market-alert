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
