"""tools/ft_probe.py: the FT's bars, asked from a runner."""
from tools import ft_probe


class _Answer:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class _Session:
    def get(self, url, params=None, headers=None, timeout=None):
        return _Answer({"data": {"security": [
            {"symbol": "LCZ26:CME", "xid": "991714947", "assetClass": "Commodities"}]}})

    def post(self, url, data=None, headers=None, timeout=None):
        if '"1046650"' in data:                                  # coffee refused
            return _Answer(ValueError("Expecting value"))
        return _Answer({"Dates": ["2026-10-09T15:00:00", "2026-10-09T16:00:00"],
                        "Elements": [{"ComponentSeries": [
                            {"Type": "Close", "Values": [240.1, 240.5]}]}]})


def test_each_series_is_saved_and_a_refusal_listed(tmp_path):
    summary = ft_probe.probe(str(tmp_path), _Session())
    assert summary[0].startswith("LC.1 (xid 1044934): HTTP 200, 2 hourly bars 2026-10-09T15:00:00")
    assert summary[1].startswith("KC.1: failed - Expecting value")
    assert summary[2].startswith("LCZ26 (xid 991714947): HTTP 200") and "closes sum 480.600" in summary[2]
    assert (tmp_path / "LCZ26.json").exists() and not (tmp_path / "KC.1.json").exists()
    assert ft_probe.closes({"Elements": None}) == []
