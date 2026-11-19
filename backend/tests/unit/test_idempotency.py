"""app/idempotency — a key is applied at most once, sequential or concurrent."""
import asyncio

import httpx
from fastapi import FastAPI

from app.idempotency import IdempotencyMiddleware


def _app():
    app = FastAPI()
    app.state.calls = 0

    @app.post("/w")
    async def w():
        app.state.calls += 1
        await asyncio.sleep(0.05)               # widen the race window
        return {"n": app.state.calls}

    app.add_middleware(IdempotencyMiddleware)
    return app


def test_sequential_and_concurrent(db_schema):
    app = _app()

    async def go():
        t = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=t, base_url="http://x") as c:
            a = await c.post("/w", headers={"Idempotency-Key": "seq-1"})
            b = await c.post("/w", headers={"Idempotency-Key": "seq-1"})
            assert a.json() == b.json() and b.headers.get("idempotent-replay") == "true"
            rs = await asyncio.gather(*[c.post("/w", headers={"Idempotency-Key": "par-1"}) for _ in range(4)])
            assert len({r.text for r in rs}) == 1
            await c.post("/w")
            await c.post("/w")                    # no key: every call runs
    asyncio.run(go())
    assert app.state.calls == 1 + 1 + 2
