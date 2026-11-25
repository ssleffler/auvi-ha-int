"""The recorder's viewer page gets its session seeded before its scripts run."""

from custom_components.auvi_nvr.proxy import PANEL_TOKEN, seed_viewer_session

VIEWER = b'<!doctype html><html><head lang="en"><script src="./early-auth.js"></script></head></html>'


def test_viewer_page_is_seeded_before_its_scripts() -> None:
    out = seed_viewer_session(VIEWER)
    assert PANEL_TOKEN.encode() in out
    assert out.index(b"auvi.refresh_token") < out.index(b"early-auth.js")


def test_other_pages_are_untouched() -> None:
    page = b"<html><head></head><body>console</body></html>"
    assert seed_viewer_session(page) == page
