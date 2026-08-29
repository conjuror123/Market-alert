from price_monitor.__main__ import append_id_footer


def test_append_id_footer_adds_visible_id():
    result = append_id_footer("Alert text here", 12345)
    assert result == "Alert text here\n\n<i>ID: 12345</i>"
