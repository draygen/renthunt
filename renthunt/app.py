"""FastAPI app: JSON API, chat stream, static UI, and the background scheduler."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Body, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import actions, ai, chat, config, db, hunt


async def scheduler() -> None:
    """Start a hunt whenever one is due. Checks twice a minute."""
    await asyncio.sleep(20)
    while True:
        try:
            due = hunt.next_run_time()
            if due and not hunt.is_running() and due <= datetime.now().astimezone():
                await asyncio.to_thread(hunt.run, "schedule")
        except Exception as exc:  # the loop must survive anything
            print(f"scheduler error: {exc!r}")
        await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    task = asyncio.create_task(scheduler())
    yield
    task.cancel()


app = FastAPI(title="RentHunt", lifespan=lifespan)


# ---------- access control ----------
# On this machine the app is fully open. Anyone arriving through a tunnel or proxy is a
# visitor: they can browse listings, and everything else needs the admin password.

LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
PROXY_HEADERS = ("cf-connecting-ip", "cf-ray", "x-forwarded-for", "x-forwarded-proto", "x-real-ip", "forwarded")
VISITOR_GET = ("/api/listings", "/api/overview", "/api/stats", "/api/me")
VISITOR_VIEWS = ("matches", "new", "all")
_failed_logins: list[float] = []


def admin_password() -> str:
    if os.environ.get("RENTHUNT_PASSWORD"):
        return os.environ["RENTHUNT_PASSWORD"]
    path = config.DATA_DIR / "admin_password"
    if not path.exists():
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_urlsafe(12) + "\n")
        path.chmod(0o600)
    return path.read_text().strip()


def _session_token() -> str:
    return hmac.new(admin_password().encode(), b"renthunt-admin-session", hashlib.sha256).hexdigest()


def is_local(request: Request) -> bool:
    host = (request.headers.get("host") or "").rsplit(":", 1)[0]
    return (
        not any(header in request.headers for header in PROXY_HEADERS)
        and request.client is not None and request.client.host in LOCAL_HOSTS
        and host in LOCAL_HOSTS
    )


def is_admin(request: Request) -> bool:
    if is_local(request):
        return True
    return hmac.compare_digest(request.cookies.get("renthunt_admin", ""), _session_token())


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    request.state.admin = is_admin(request)
    if path.startswith("/api/") and not request.state.admin:
        allowed = path == "/api/login" or (request.method == "GET" and path.startswith(VISITOR_GET))
        if not allowed:
            return JSONResponse({"detail": "Sign in to do that."}, status_code=401)
    return await call_next(request)


@app.get("/api/me")
def me(request: Request):
    return {"admin": request.state.admin, "local": is_local(request)}


@app.post("/api/login")
def login(request: Request, response: Response, body: dict = Body(...)):
    now = time.time()
    _failed_logins[:] = [t for t in _failed_logins if now - t < 600]
    if len(_failed_logins) >= 8:
        raise HTTPException(429, "Too many wrong passwords. Try again in a few minutes.")
    if not hmac.compare_digest(str(body.get("password") or "").encode(), admin_password().encode()):
        _failed_logins.append(now)
        time.sleep(1)
        raise HTTPException(401, "Wrong password.")
    response.set_cookie(
        "renthunt_admin", _session_token(), max_age=30 * 86400, httponly=True, samesite="lax",
        secure=request.headers.get("x-forwarded-proto") == "https",
    )
    return {"admin": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie("renthunt_admin")
    return {"admin": False}


def _for_visitor(row: dict) -> dict:
    """Strip the owner's private marks from a listing."""
    row.update(notes="", favorite=False, hidden=False, status="new" if row["status"] == "new" else "seen")
    return row


def _split(value: str | None) -> list[str] | None:
    return [v for v in value.split(",") if v] if value else None


@app.get("/api/listings")
def listings(
    request: Request,
    view: str = "matches", towns: str | None = None, sources: str | None = None,
    min_price: int | None = None, max_price: int | None = None,
    min_beds: float | None = None, max_beds: float | None = None,
    text: str | None = None, sort: str = "score", ids: str | None = None,
    limit: int = Query(300, le=1000), offset: int = 0,
):
    admin = request.state.admin
    if ids:  # an explicit set, in the order asked for (used to show the assistant's results)
        wanted = [int(i) for i in ids.split(",") if i.strip().isdigit()][:100]
        rows = db.get_listings_by_ids(wanted)
        for row in rows:
            row["description"] = (row["description"] or "")[:300]
            if not admin:
                _for_visitor(row)
        return {"total": len(rows), "listings": rows}
    if not admin and view not in VISITOR_VIEWS:
        view = "matches"
    rows, total = db.query_listings(
        search_notes=admin,
        view=view, towns=_split(towns), sources=_split(sources), min_price=min_price,
        max_price=max_price, min_beds=min_beds, max_beds=max_beds, text=text or None,
        sort=sort, limit=limit, offset=offset,
    )
    for row in rows:
        row["description"] = (row["description"] or "")[:300]
        if not admin:
            _for_visitor(row)
    return {"total": total, "listings": rows}


@app.get("/api/listings/{listing_id}")
def listing(listing_id: int, request: Request):
    found = db.get_listing(listing_id)
    if not found:
        raise HTTPException(404, "No such listing")
    return found if request.state.admin else _for_visitor(found)


@app.patch("/api/listings/{listing_id}")
def patch_listing(listing_id: int, patch: dict = Body(...)):
    try:
        found = db.update_listing(listing_id, **patch)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not found:
        raise HTTPException(404, "No such listing")
    return found


@app.post("/api/listings/mark-seen")
def mark_seen():
    with db.connect() as conn:
        conn.execute("UPDATE listings SET status='seen' WHERE status='new'")
    return db.counts()


@app.get("/api/overview")
def overview(request: Request):
    data = actions.overview()
    if not request.state.admin:
        data["places"] = []  # where the owner's people live is not for visitors
        data["counts"].update(favorites=0, pipeline=0, hidden=0)
        data["ai"] = {"ok": data["ai"]["ok"], "model": "", "model_installed": True}
    return data


@app.get("/api/settings")
def get_settings():
    settings = db.get_settings()
    return {"settings": settings, "watches": actions.list_watches(), "ai": ai.status(settings, fresh=True)}


@app.put("/api/settings")
def put_settings(settings: dict = Body(...)):
    current = db.get_settings()
    for key in config.DEFAULT_SETTINGS:
        if key in settings:
            current[key] = settings[key]
    db.save_settings(current)
    hunt.rescore_all()
    return {"settings": db.get_settings(), "counts": db.counts()}


@app.post("/api/towns")
def add_town(body: dict = Body(...)):
    result = actions.set_towns(add=[body])
    if result["problems"]:
        raise HTTPException(400, result["problems"][0])
    return {"settings": db.get_settings()}


@app.post("/api/places")
def add_place(body: dict = Body(...)):
    try:
        actions.add_place(body.get("name", ""), body.get("where", ""))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return db.get_settings()["places"]


@app.delete("/api/places/{name}")
def delete_place(name: str):
    actions.remove_place(name)
    return db.get_settings()["places"]


@app.post("/api/watches")
def add_watch(body: dict = Body(...)):
    try:
        actions.add_watch(body.get("url", ""), body.get("label", ""))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return actions.list_watches()


@app.delete("/api/watches/{watch_id}")
def delete_watch(watch_id: int):
    with db.connect() as conn:
        conn.execute("DELETE FROM watch_urls WHERE id=?", (watch_id,))
        conn.execute("UPDATE listings SET active=0 WHERE source='watch' AND source_id LIKE ?", (f"w{watch_id}-%",))
    return actions.list_watches()


@app.post("/api/hunt")
def start_hunt():
    return {"started": actions.start_hunt("manual")}


@app.get("/api/runs")
def runs():
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 30").fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["summary"] = json.loads(item["summary"]) if item["summary"] else {}
        out.append(item)
    return out


@app.get("/api/stats")
def stats():
    return actions.market_stats()


@app.get("/api/chat/{session_id}")
def chat_history(session_id: str):
    return [m for m in chat.load_history(session_id)
            if m["role"] in ("user", "assistant") and m.get("content")]


@app.post("/api/chat-warm")
async def chat_warm():
    """Load the model ahead of the first question so the first answer is not slow."""
    import httpx

    settings = db.get_settings()
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            await client.post(f"{ai.base_url(settings)}/api/chat",
                              json=ai.chat_payload(settings, [], stream=False))
    except Exception as exc:
        return {"ok": False, "error": ai.describe_error(exc)}
    return {"ok": True}


@app.delete("/api/chat/{session_id}")
def chat_clear(session_id: str):
    chat.clear_history(session_id)
    return {"ok": True}


@app.post("/api/chat/{session_id}")
async def chat_send(session_id: str, body: dict = Body(...)):
    text = (body.get("message") or "").strip()
    if not text:
        raise HTTPException(400, "Empty message")

    async def events():
        async for event in chat.respond(session_id, text):
            yield json.dumps(event) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/")
def index():
    return FileResponse(config.STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")


def main() -> None:
    import os

    import uvicorn

    uvicorn.run(app, host=os.environ.get("RENTHUNT_HOST", "127.0.0.1"),
                port=int(os.environ.get("RENTHUNT_PORT", "8642")), log_level="warning")


if __name__ == "__main__":
    main()
