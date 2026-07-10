import logging

# Configure logging before any router imports so app-level INFO logs surface
# in both uvicorn dev and production stderr streams.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from app.api.actions import router as actions_router
from app.api.auth import router as auth_router
from app.api.buckets import router as buckets_router
from app.api.gmail import router as gmail_router
from app.api.inbox import router as inbox_router
from app.api.jobs import router as jobs_router
from app.api.search import router as search_router
from app.api.sse import router as sse_router
from app.api.sync import router as sync_router
from app.api.tasks import router as tasks_router
from app.config import get_settings
from app.realtime import pubsub


settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The dispatcher is per-process: each uvicorn worker holds its own redis
    # pubsub connection and routes incoming messages to its local SSE queues.
    # The internal task waits on _has_subscription before touching redis, so
    # apps that boot without ever opening an SSE connection (e.g. test runs
    # that don't hit /api/sse) make zero network calls.
    await pubsub.start()
    try:
        yield
    finally:
        await pubsub.stop()


app = FastAPI(title="inbox_enhanced", lifespan=lifespan)
app.include_router(actions_router)
app.include_router(auth_router)
app.include_router(buckets_router)
app.include_router(gmail_router)
app.include_router(inbox_router)
app.include_router(jobs_router)
app.include_router(search_router)
app.include_router(sse_router)
app.include_router(sync_router)
app.include_router(tasks_router)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "env": settings.env}


_STATIC_DIR = Path(__file__).parent / "static"
_INDEX = _STATIC_DIR / "index.html"

# Mount built bundle assets at /assets (vite emits hashed files there).
_assets = _STATIC_DIR / "assets"
if _assets.is_dir():
    app.mount("/assets", StaticFiles(directory=str(_assets)), name="assets")


@app.get("/{full_path:path}", include_in_schema=False)
def spa_catch_all(full_path: str, request: Request):
    # Reserved API/auth/asset paths are handled by their own routers (and 404 here is fine
    # because FastAPI matches more-specific routes first). This catch-all only fires for
    # the leftover routes — i.e. SPA routes.
    if full_path.startswith(("api/", "auth/", "assets/")):
        return JSONResponse({"detail": "not found"}, status_code=404)
    if _INDEX.exists():
        return FileResponse(_INDEX)
    # Helpful when the frontend hasn't been built yet.
    return JSONResponse(
        {"detail": "frontend not built. Run scripts/build_frontend.sh."},
        status_code=503,
    )
