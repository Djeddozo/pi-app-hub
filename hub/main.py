import asyncio
import os
import subprocess
import threading
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import config
from .discovery import Manifest
from .manager import Manager
from .registry import Registry
from .security import safe_path, safe_url
from .storage import Storage, tree
from .system import System

class Metadata(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=100)
    icon: str | None = Field(default=None, pattern=r"^(box|wallet|activity|calendar|car|message|database|grid|globe|code)$")
    project_path: str | None = None
    web_url: str | None = None
    health_check: str | None = Field(default=None, max_length=200)
    data_directories: list[str] | None = Field(default=None, max_length=30)
    protected: bool | None = None
    _path = field_validator("project_path")(safe_path)
    _url = field_validator("web_url")(safe_url)

    @field_validator("data_directories")
    @classmethod
    def directories(cls, values):
        return [safe_path(v) for v in values] if values is not None else None

class Uninstall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(max_length=100)
    confirmation: str = Field(max_length=100)

class CachePreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[str] = Field(min_length=1, max_length=10)

class CacheClean(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(max_length=100)
    confirmation: str = Field(max_length=100)

def create_app(data=config.DATA, background=True):
    os.umask(0o077)
    registry = Registry(data / "registry.sqlite3")
    manifests = data / "manifests"
    manifests.mkdir(exist_ok=True, mode=0o700)
    system = System()
    manager = Manager(registry, system, manifests)
    storage = Storage(data / "storage.json")

    @asynccontextmanager
    async def lifespan(app):
        if background:
            manager.start()
            threading.Thread(target=storage.run_schedule, daemon=True).start()
        yield
        if background:
            manager.close()
            storage.close()

    app = FastAPI(title="Pi App Hub", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.manager = manager
    app.state.registry = registry
    app.state.storage = storage
    allowed_hosts = {"127.0.0.1:8840", "localhost:8840", urlsplit(config.ORIGIN).netloc}
    allowed_origins = {"http://127.0.0.1:8840", "http://localhost:8840", config.ORIGIN}

    @app.middleware("http")
    async def security(request: Request, call_next):
        if request.headers.get("host") not in allowed_hosts:
            return JSONResponse({"detail": "Untrusted host"}, 400)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("x-pi-hub") != "1" or request.headers.get("origin", config.ORIGIN) not in allowed_origins or request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "Cross-origin administration is blocked"}, 403)
            try:
                if int(request.headers.get("content-length", "0")) > 32768:
                    return JSONResponse({"detail": "Request too large"}, 413)
            except ValueError:
                return JSONResponse({"detail": "Invalid content length"}, 400)
        response = await call_next(request)
        response.headers.update({"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            "Cache-Control": "no-store" if request.url.path.startswith("/api") else "no-cache"})
        return response

    def admin(request, action, app_id, operation):
        actor = request.headers.get("tailscale-user-login", "local")
        registry.audit(actor, action, app_id, "requested")
        try:
            result = operation()
            registry.audit(actor, action, app_id, "success")
            return result or {"ok": True}
        except ValueError as exc:
            registry.audit(actor, action, app_id, "failed", str(exc))
            raise HTTPException(409, str(exc)) from exc
        except Exception as exc:
            registry.audit(actor, action, app_id, "failed", type(exc).__name__)
            raise HTTPException(503, "Operation could not complete. Some members may already have changed; scan and check their status.") from exc

    @app.get("/api/system")
    def stats():
        return {**system.stats(), "discovery": manager.health, "last_scan": manager.last_scan}

    @app.get("/api/processes")
    def processes():
        return system.processes()

    @app.get("/api/apps")
    def apps():
        return manager.apps()

    @app.get("/api/apps/{app_id}")
    def details(app_id: str):
        item = next((a for a in manager.apps() if a["id"] == app_id), None)
        if not item:
            raise HTTPException(404, "Application not found")
        return item

    @app.post("/api/scan")
    def scan(request: Request):
        return admin(request, "scan", None, manager.scan)

    @app.patch("/api/apps/{app_id}")
    def edit(app_id: str, metadata: Metadata, request: Request):
        if not registry.get(app_id):
            raise HTTPException(404, "Application not found")
        changes = metadata.model_dump(exclude_unset=True)
        if any(changes.get(k, "unset") is None for k in ("name", "icon", "protected")):
            raise HTTPException(422, "Name, icon and protected cannot be null")
        if changes.get("data_directories") is not None:
            changes["data_directories"] = [{"type": "directory", "path": p} for p in changes["data_directories"]]
        elif "data_directories" in changes:
            changes["data_directories"] = []
        return admin(request, "edit metadata", app_id, lambda: registry.edit(app_id, changes))

    @app.post("/api/apps/register")
    def register(manifest: Manifest, request: Request):
        def save():
            path = manifests / (manifest.id + ".json")
            try:
                with path.open("x") as file:
                    file.write(manifest.model_dump_json(indent=2))
            except FileExistsError as exc:
                raise ValueError("A manifest with that ID already exists") from exc
            return manager.scan()
        return admin(request, "register", None, save)

    @app.post("/api/apps/{app_id}/actions/{action}")
    async def action(app_id: str, action: str, request: Request):
        return await asyncio.to_thread(admin, request, action, app_id, lambda: manager.action(app_id, action))

    @app.get("/api/apps/{app_id}/logs")
    def logs(app_id: str, lines: int = 150):
        try:
            return {"text": manager.logs(app_id, min(max(lines, 1), 500))}
        except Exception as exc:
            raise HTTPException(503, "Logs unavailable; check the discovery source and journal permissions") from exc

    @app.post("/api/apps/{app_id}/uninstall-preview")
    def preview(app_id: str, request: Request):
        return admin(request, "uninstall preview", app_id, lambda: manager.preview(app_id))

    @app.post("/api/apps/{app_id}/uninstall")
    async def uninstall(app_id: str, body: Uninstall, request: Request):
        return await asyncio.to_thread(admin, request, "uninstall", app_id, lambda: manager.uninstall(app_id, body.token, body.confirmation))

    @app.get("/api/storage")
    def storage_state():
        return storage.status()

    tree_limit = threading.Semaphore(2)

    @app.get("/api/storage/tree")
    def storage_tree(path: str):
        if not tree_limit.acquire(blocking=False):
            raise HTTPException(429, "Another folder is still loading; try again in a moment.")
        try:
            return tree(path)
        except (OSError, ValueError) as exc:
            raise HTTPException(400, str(exc)[:300]) from exc
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(503, "Folder scan took too long; open a smaller folder.") from exc
        finally:
            tree_limit.release()

    @app.post("/api/storage/cleanup/preview")
    def storage_preview(body: CachePreview, request: Request):
        return admin(request, "storage cleanup preview", None, lambda: storage.preview(body.ids))

    @app.post("/api/storage/cleanup")
    async def storage_clean(body: CacheClean, request: Request):
        return await asyncio.to_thread(admin, request, "storage cleanup", None, lambda: storage.clean(body.token, body.confirmation))

    @app.post("/api/storage/{action}", status_code=202)
    def storage_start(action: str, request: Request):
        if action not in {"scan", "advice"}:
            raise HTTPException(404, "Not found")
        return admin(request, f"storage {action}", None, lambda: storage.start(action))

    @app.get("/api/activity")
    def activity():
        return registry.activity()

    dist = config.ROOT / "frontend" / "dist"
    if dist.is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}")
        def frontend(path: str):
            if path.startswith("api/"):
                raise HTTPException(404, "Not found")
            if path in {"manifest.webmanifest", "sw.js", "offline.html", "icon.svg", "icon-192.png", "icon-512.png"}:
                media = "application/manifest+json" if path.endswith("webmanifest") else "application/javascript" if path == "sw.js" else None
                return FileResponse(dist / path, media_type=media)
            return FileResponse(dist / "index.html")
    return app
