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


async def test_detection_streams_share_one_websocket(socket_enabled, aiohttp_client) -> None:
    """Two cameras' event streams arrive tagged on one socket; unsub and end work."""
    import asyncio

    from aiohttp import web

    from custom_components.auvi_nvr.proxy import relay_detections

    gates: dict[str, asyncio.Event] = {}

    class Upstream:
        status = 200

        def __init__(self, cam: str) -> None:
            self.cam, self.content, self.closed = cam, self, False

        async def iter_any(self):
            yield f"data: {self.cam}\n\n".encode()
            await gates.setdefault(self.cam, asyncio.Event()).wait()

        def close(self) -> None:
            self.closed = True

    opened: list[Upstream] = []

    async def open_stream(cam: str) -> Upstream:
        opened.append(Upstream(cam))
        return opened[-1]

    async def handler(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await relay_detections(ws, open_stream)
        return ws

    app = web.Application()
    app.router.add_get("/ws", handler)
    ws = await (await aiohttp_client(app)).ws_connect("/ws")

    await ws.send_json({"sub": "a"})
    await ws.send_json({"sub": "b"})
    got = {m["cam"]: m["data"] for m in [await ws.receive_json(), await ws.receive_json()]}
    assert got == {"a": "data: a\n\n", "b": "data: b\n\n"}

    gates["a"].set()  # the recorder ends a's stream
    assert await ws.receive_json() == {"cam": "a", "end": True}
    await ws.send_json({"sub": "a"})  # the page subscribes again
    assert (await ws.receive_json())["cam"] == "a"

    await ws.send_json({"unsub": "b"})
    await ws.close()
    await asyncio.sleep(0.05)
    assert all(u.closed for u in opened) and len(opened) == 3
