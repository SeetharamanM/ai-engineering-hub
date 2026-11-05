"""FastAPI application serving the hub UI and its JSON API."""

from __future__ import annotations

import html
import ipaddress
import mimetypes
import os
import platform
import re
import secrets
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import markdown
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .discovery import Project, discover, strip_boilerplate, _find_readme, _read
from .envfiles import mask, read_env, update_env
from .runner import ProcessManager, project_python

STATIC_DIR = Path(__file__).parent / "static"
ASSET_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".mp4", ".webm", ".pdf"}


class Settings:
    def __init__(self, repo_root: Path, host: str = "127.0.0.1", token: str | None = None,
                 data_dir: Path | None = None):
        self.repo_root = repo_root.resolve()
        self.host = host
        self.token = token
        self.data_dir = (data_dir or (self.repo_root / "hub" / ".hub-data")).resolve()
        self.global_env_file = self.data_dir / "secrets.env"


class EnvUpdate(BaseModel):
    values: dict[str, str | None]


class RunRequest(BaseModel):
    entrypoint: str


class InstallRequest(BaseModel):
    venv: bool = True


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


# --------------------------------------------------------------------- README
_DANGEROUS_TAGS = re.compile(r"<\s*(script|iframe|object|embed|style|form|meta|link|base)\b.*?(?:</\s*\1\s*>|/?>)",
                             re.I | re.S)
_EVENT_ATTR = re.compile(r"\s+on[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.I)
_JS_URL = re.compile(r"(href|src)\s*=\s*([\"'])\s*(javascript|vbscript|data):[^\"']*\2", re.I)
_GH_BLOB = re.compile(r"https://github\.com/[^/]+/ai-engineering(?:-hub)?/(?:blob|raw)/[^/]+/([^\"')\s?#]+)(?:\?raw=true)?")


def render_readme(project: Project, repo_root: Path) -> str:
    path = _find_readme(project.path)
    if not path:
        return "<p class='muted'>This project has no README.</p>"
    md_text = strip_boilerplate(_read(path))
    # The page header already shows the title, so drop the README's leading H1.
    md_text = re.sub(r"\A\s*#\s+[^\n]*\n", "", md_text, count=1)
    body = markdown.markdown(md_text, extensions=["fenced_code", "tables", "sane_lists"])
    body = _DANGEROUS_TAGS.sub("", body)
    body = _EVENT_ATTR.sub("", body)
    body = _JS_URL.sub(r'\1="#"', body)

    def asset_url(rel: str) -> str | None:
        target = (path.parent / rel).resolve()
        try:
            rel_to_project = target.relative_to(project.path.resolve()).as_posix()
        except ValueError:
            return None
        if target.is_file() and target.suffix.lower() in ASSET_EXT:
            return f"/api/projects/{project.id}/files/{rel_to_project}"
        return None

    def gh_local(m: re.Match) -> str:
        repo_rel = m.group(1)
        target = (repo_root / repo_rel).resolve()
        if target.is_file() and target.is_relative_to(project.path.resolve()):
            return f"/api/projects/{project.id}/files/{target.relative_to(project.path.resolve()).as_posix()}"
        return m.group(0)

    body = _GH_BLOB.sub(gh_local, body)

    def rewrite(m: re.Match) -> str:
        attr, quote, url = m.group(1), m.group(2), m.group(3)
        if re.match(r"^(?:[a-z]+:|/|#)", url, re.I):
            return m.group(0)
        local = asset_url(html.unescape(url.split("#")[0]))
        return f"{attr}={quote}{local}{quote}" if local else m.group(0)

    body = re.sub(r"(src|href)=([\"'])([^\"']+)\2", rewrite, body)
    body = re.sub(r"<a (?=href=\"https?:)", '<a target="_blank" rel="noopener noreferrer" ', body)
    return body


# ------------------------------------------------------------------------ app
def create_app(settings: Settings) -> FastAPI:
    pm = ProcessManager(app_host=settings.host, global_env_file=settings.global_env_file)
    state = {"projects": discover(settings.repo_root)}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        pm.shutdown()

    app = FastAPI(title="AI Engineering Hub", lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
    app.state.pm = pm
    app.state.settings = settings

    @app.middleware("http")
    async def auth_and_headers(request: Request, call_next):
        path = request.url.path
        if settings.token and path.startswith("/api/") and path != "/api/auth":
            supplied = request.headers.get("x-hub-token") or request.cookies.get("hub_token") or ""
            if not secrets.compare_digest(supplied, settings.token):
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        if request.method not in ("GET", "HEAD", "OPTIONS") and path.startswith("/api/"):
            # Basic CSRF protection: state-changing calls must come from this origin.
            origin = request.headers.get("origin")
            if origin and origin.split("://", 1)[-1] != request.headers.get("host", ""):
                return JSONResponse({"detail": "Cross-origin request refused"}, status_code=403)
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    def get_project(pid: str) -> Project:
        p = state["projects"].get(pid)
        if not p:
            raise HTTPException(404, f"Unknown project {pid!r}")
        return p

    def summarize(p: Project) -> dict:
        d = p.to_dict()
        proj_env = read_env(p.path / ".env")
        glob_env = read_env(settings.global_env_file)
        d["env_missing"] = [k for k in p.env_vars if not (proj_env.get(k) or glob_env.get(k) or os.environ.get(k))]
        d["has_venv"] = (p.path / ".venv").exists()
        d["running"] = [x.to_dict() for x in pm.active_for(p.id)]
        return d

    # ------------------------------------------------------------- endpoints
    @app.post("/api/auth")
    async def auth(request: Request):
        body = await request.json()
        if not settings.token:
            return {"ok": True, "required": False}
        if not secrets.compare_digest(str(body.get("token", "")), settings.token):
            raise HTTPException(401, "Invalid token")
        resp = JSONResponse({"ok": True, "required": True})
        resp.set_cookie("hub_token", settings.token, httponly=True, samesite="strict")
        return resp

    @app.get("/api/system")
    def system():
        def reachable(port: int) -> bool:
            with socket.socket() as s:
                s.settimeout(0.3)
                return s.connect_ex(("127.0.0.1", port)) == 0

        return {
            "python": sys.version.split()[0],
            "platform": platform.platform(terse=True),
            "repo_root": str(settings.repo_root),
            "app_host": settings.host,
            "ollama_running": reachable(11434),
            "auth_required": bool(settings.token),
            "project_count": len(state["projects"]),
        }

    @app.get("/api/projects")
    def list_projects():
        return [summarize(p) for p in state["projects"].values()]

    @app.post("/api/projects/rescan")
    def rescan():
        state["projects"] = discover(settings.repo_root)
        return {"count": len(state["projects"])}

    @app.get("/api/projects/{pid}")
    def project_detail(pid: str):
        p = get_project(pid)
        d = summarize(p)
        d["readme_html"] = render_readme(p, settings.repo_root)
        d["python"] = project_python(p)
        d["requirements_files"] = p.requirements_files
        return d

    @app.get("/api/projects/{pid}/files/{file_path:path}")
    def project_file(pid: str, file_path: str):
        p = get_project(pid)
        root = p.path.resolve()
        target = (root / file_path).resolve()
        if not target.is_relative_to(root) or not target.is_file() or target.suffix.lower() not in ASSET_EXT:
            raise HTTPException(404, "Not found")
        media = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        headers = {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox"}
        return FileResponse(target, media_type=media, headers=headers)

    def env_view(keys: list[str], file_env: dict[str, str], glob_env: dict[str, str], scope: str) -> list[dict]:
        out = []
        for k in sorted(set(keys) | set(file_env)):
            if file_env.get(k):
                src, val = scope, file_env[k]
            elif scope == "project" and glob_env.get(k):
                src, val = "global", glob_env[k]
            elif os.environ.get(k):
                src, val = "system", os.environ[k]
            else:
                src, val = None, ""
            out.append({"name": k, "set": bool(val), "source": src, "preview": mask(val),
                        "expected": k in keys})
        return out

    @app.get("/api/projects/{pid}/env")
    def get_project_env(pid: str):
        p = get_project(pid)
        return env_view(p.env_vars, read_env(p.path / ".env"), read_env(settings.global_env_file), "project")

    @app.put("/api/projects/{pid}/env")
    def put_project_env(pid: str, body: EnvUpdate):
        p = get_project(pid)
        try:
            update_env(p.path / ".env", body.values)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return get_project_env(pid)

    @app.get("/api/settings/env")
    def get_global_env():
        known = sorted({k for p in state["projects"].values() for k in p.env_vars})
        return env_view(known, read_env(settings.global_env_file), {}, "global")

    @app.put("/api/settings/env")
    def put_global_env(body: EnvUpdate):
        try:
            update_env(settings.global_env_file, body.values)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return get_global_env()

    @app.post("/api/projects/{pid}/install")
    def install(pid: str, body: InstallRequest):
        p = get_project(pid)
        return pm.install(p, use_venv=body.venv).to_dict()

    @app.post("/api/projects/{pid}/run")
    def run(pid: str, body: RunRequest):
        p = get_project(pid)
        ep = next((e for e in p.entrypoints if e.id == body.entrypoint), None)
        if not ep:
            raise HTTPException(404, "Unknown entrypoint")
        try:
            return pm.launch(p, ep).to_dict()
        except RuntimeError as e:
            raise HTTPException(503, str(e))

    @app.get("/api/processes")
    def processes():
        return [p.to_dict() for p in pm.list()]

    def get_proc(proc_id: str):
        proc = pm.get(proc_id)
        if not proc:
            raise HTTPException(404, "Unknown process")
        return proc

    @app.get("/api/processes/{proc_id}")
    def process(proc_id: str):
        return get_proc(proc_id).to_dict()

    @app.get("/api/processes/{proc_id}/logs")
    def process_logs(proc_id: str, after: int = -1):
        proc = get_proc(proc_id)
        lines, last = proc.logs_after(after)
        return {"lines": lines, "last": last, "process": proc.to_dict()}

    @app.post("/api/processes/{proc_id}/stop")
    def stop(proc_id: str):
        get_proc(proc_id)
        return pm.stop(proc_id).to_dict()

    @app.delete("/api/processes/{proc_id}")
    def remove(proc_id: str):
        get_proc(proc_id)
        if not pm.remove(proc_id):
            raise HTTPException(409, "Process is still running")
        return {"ok": True}

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def make_settings_from_env() -> Settings:
    repo = Path(os.environ.get("HUB_REPO_ROOT", Path(__file__).resolve().parents[2]))
    host = os.environ.get("HUB_HOST", "127.0.0.1")
    token = os.environ.get("HUB_TOKEN") or None
    if not token and not _is_loopback(host) and os.environ.get("HUB_ALLOW_NO_AUTH") != "1":
        token = secrets.token_urlsafe(18)
        os.environ["HUB_TOKEN"] = token
    return Settings(repo_root=repo, host=host, token=token)
