"""tools/hot_bars.sh: saving the open months of the bar store to the release.
Run against a stand-in for gh that records what it was asked."""
import os
import stat
import subprocess

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "tools", "hot_bars.sh")

GH = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$GH_LOG"
case "$*" in
  *"releases/tags/"*) echo 1 ;;
  *"/assets?"*) printf 'bars-live-9-1.tar.gz\\t8\\nbars-live-old.tar.gz\\t7\\n' ;;
esac
"""


def _save(tmp_path, open_months):
    bin_dir, work = tmp_path / "bin", tmp_path / "work"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(GH)
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    for name in open_months:
        (work / name).parent.mkdir(parents=True, exist_ok=True)
        (work / name).write_text("hour_utc,open,high,low,close,volume\n")
    (work / "data/jump/bars").mkdir(parents=True, exist_ok=True)
    log = tmp_path / "gh.log"
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", GH_LOG=str(log),
               GITHUB_REPOSITORY="o/r", GITHUB_REF_NAME="live", GITHUB_RUN_ID="9")
    out = subprocess.run(["bash", os.path.abspath(SCRIPT), "save"], cwd=work, env=env,
                         capture_output=True, text=True)
    return out, log.read_text().splitlines() if log.exists() else []


def test_a_store_with_open_months_is_saved_and_the_old_archive_goes(tmp_path):
    out, asked = _save(tmp_path, ["data/jump/bars/binance_BTC_USDT/2026-10.open.csv"])
    assert out.returncode == 0, out.stderr
    assert any(a.startswith("release upload bars-live-live") for a in asked)
    assert any("-X DELETE" in a and a.endswith("/assets/7") for a in asked)


def test_no_open_months_on_disk_is_not_saved_over_the_last_archive(tmp_path):
    # An empty archive uploaded, and the good one deleted after it, would have
    # the next run restore nothing: every recent move read as gone.
    out, asked = _save(tmp_path, [])
    assert out.returncode != 0
    assert "no open months" in out.stdout
    assert asked == []


RESTORE_GH = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$GH_LOG"
case "$*" in
  *"releases/tags/bars-live-claude-live"*) echo 1 ;;
  *"releases/tags/"*) echo "gh: Not Found (HTTP 404)"; exit 1 ;;
  *"/assets?"*) printf 'bars-live-9-1.tar.gz\\t8\\n' ;;
  "release download"*)
    a="$*"; dir="${a##*-D }"; dir="${dir%% *}"
    tar -czf "$dir/bars-live-9-1.tar.gz" -C "$ARCHIVE" data ;;
esac
"""


def test_a_restore_reads_the_named_branchs_open_months(tmp_path):
    # A replay on a branch with no archive of its own read none: every move of
    # the open months was gone from it.
    bin_dir, work, archive = tmp_path / "bin", tmp_path / "work", tmp_path / "archive"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(RESTORE_GH)
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    month = "data/jump/bars/binance_BTC_USDT/2026-10.open.csv"
    (archive / month).parent.mkdir(parents=True)
    (archive / month).write_text("hour_utc,open,high,low,close,volume\n")
    (work / "data/jump/bars").mkdir(parents=True)
    log = tmp_path / "gh.log"
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", GH_LOG=str(log),
               ARCHIVE=str(archive), GITHUB_REPOSITORY="o/r", GITHUB_REF_NAME="dev",
               BARS_BRANCH="claude/live")
    out = subprocess.run(["bash", os.path.abspath(SCRIPT), "restore"], cwd=work, env=env,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert (work / month).exists()
    assert "release download bars-live-claude-live" in log.read_text()
