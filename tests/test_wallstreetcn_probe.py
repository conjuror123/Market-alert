"""tools/wallstreetcn_probe.py: Wallstreetcn's bars, asked from a runner."""
from tools import wallstreetcn_probe


class _Answer:
    status_code = 200

    def __init__(self, code):
        self.code = code

    def json(self):
        if self.code == "UKNI.OTC":
            raise ValueError("Expecting value")
        return {"data": {"candle": {self.code: {"lines": [[1.0, 2.0, 3.0, 0.5, 1791565200]]}}}}


class _Session:
    def get(self, url, params=None, headers=None, timeout=None):
        return _Answer(params["prod_code"])


def test_each_code_is_saved_and_a_refusal_listed(tmp_path):
    summary = wallstreetcn_probe.probe(str(tmp_path), _Session())
    assert summary[0] == "UKSN.OTC: HTTP 200, 1 hourly bars 2026-10-09 17:00 .. 2026-10-09 17:00 UTC"
    assert summary[1].startswith("UKNI.OTC: failed - Expecting value")
    assert (tmp_path / "UKSN.OTC.json").exists() and not (tmp_path / "UKNI.OTC.json").exists()
    assert wallstreetcn_probe.lines({"data": None}, "UKSN.OTC") == []
