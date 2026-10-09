"""tools/eastmoney_probe.py: Eastmoney's bars, asked from a runner."""
from tools import eastmoney_probe


class _Answer:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def json(self):
        return self.payload


class _Session:
    """Refuses the first host, answers the second."""

    def __init__(self):
        self.asked = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.asked.append((url, params["secid"], params["klt"]))
        if "//push2his." in url:
            raise ConnectionError("tunnel closed")
        return _Answer({"data": {"klines": ["2026-10-09 15:00,52900,53000,53050,52880,12,0"]}})


def test_a_beijing_stamp_reads_as_utc():
    got = eastmoney_probe.parse({"data": {"klines": ["2026-10-09 15:00,1,2,3,0.5,9,0",
                                                     "2026-10-09,1,2,3,0.5,9,0"]}})
    # 15:00 in Beijing is 07:00 UTC; a daily bar is its date's midnight there.
    assert got[0] == (1791529200, 1.0, 3.0, 0.5, 2.0)
    assert got[1][0] == 1791475200
    assert eastmoney_probe.parse({"data": None}) == []


def test_every_host_is_asked_and_the_first_answer_saved(tmp_path):
    session = _Session()
    lines = eastmoney_probe.probe(str(tmp_path), session)
    assert any("push2his.eastmoney.com 109.LTNT klt=60: failed" in line for line in lines)
    assert any(line.startswith("33.push2his.eastmoney.com 109.LTNT klt=60") and "1 bars" in line
               for line in lines)
    assert (tmp_path / "109.LTNT_60.json").exists() and (tmp_path / "107.SPY_101.json").exists()
    assert (tmp_path / "summary.txt").read_text().count("\n") == len(lines)
