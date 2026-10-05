"""tools/hot_bars.sh: laying the open months of the bar store down."""
import os
import subprocess
import tarfile

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "tools", "hot_bars.sh")


def _archive(path, members):
    with tarfile.open(path, "w:gz") as tar:
        for name in members:
            info = tarfile.TarInfo(name)
            info.size = 0
            tar.addfile(info)


def _unpack(tmp_path, members):
    _archive(tmp_path / "a.tar.gz", members)
    work = tmp_path / "work"
    work.mkdir()
    env = dict(os.environ, GITHUB_REPOSITORY="o/r", GITHUB_REF_NAME="live")
    subprocess.run(["bash", os.path.abspath(SCRIPT), "unpack", str(tmp_path / "a.tar.gz")],
                   cwd=work, env=env, check=True, capture_output=True)
    return work


def test_an_archive_saved_before_the_rename_lands_in_the_new_store(tmp_path):
    # Production's archive holds data/tremor/bars/...: laid down there, the
    # store would miss its last weeks and every recent move would read as gone.
    work = _unpack(tmp_path, ["data/tremor/bars/yahoo_UGA/2026-10.open.csv"])
    assert (work / "data/jump/bars/yahoo_UGA/2026-10.open.csv").exists()
    assert not (work / "data/tremor").exists()


def test_an_archive_saved_after_it_lands_as_it_is(tmp_path):
    work = _unpack(tmp_path, ["data/jump/bars/yahoo_UGA/2026-10.open.csv"])
    assert (work / "data/jump/bars/yahoo_UGA/2026-10.open.csv").exists()
