"""Serve finished workbooks from disk so a large Excel file is not held in RAM."""

from __future__ import annotations

import gc
import os
import re
import secrets
import shutil
from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, Response
from starlette.routing import Route

DOWNLOAD_DIR = Path(os.environ.get("RBWS_DOWNLOAD_DIR", "/tmp/rbws-workbooks"))
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{8,80}")
_ROUTE_PATH = "/rbws-download/{token}"
_ROUTE_READY = False


def publish_workbook(src: Path, download_name: str) -> str:
    """Copy the workbook out of the temp job folder. Returns the download token."""
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(18)
    dest = DOWNLOAD_DIR / f"{token}.xlsx"
    shutil.copy2(src, dest)
    (DOWNLOAD_DIR / f"{token}.name").write_text(download_name, encoding="utf-8")
    _trim_old_workbooks()
    ensure_download_route()
    gc.collect()
    return token


async def _download(request: Request) -> Response:
    token = request.path_params.get("token", "")
    if not _TOKEN_RE.fullmatch(token):
        return Response("This download expired. Generate the workbook again.", status_code=404)
    path = DOWNLOAD_DIR / f"{token}.xlsx"
    name_path = DOWNLOAD_DIR / f"{token}.name"
    if not path.is_file():
        return Response(
            "The workbook is no longer on the server. Generate it again.",
            status_code=404,
        )
    name = "RBWS.xlsx"
    if name_path.is_file():
        name = name_path.read_text(encoding="utf-8").strip() or name
    # application/zip is already compressed, so the server does not gzip it into RAM.
    return FileResponse(
        path,
        media_type="application/zip",
        filename=name,
        content_disposition_type="attachment",
    )


def _streamlit_server_app():
    """The running Streamlit server, which mounts its page at '/'."""
    from starlette.applications import Starlette

    for obj in gc.get_objects():
        try:
            app = obj if isinstance(obj, Starlette) else None
        except Exception:
            continue
        if app is None:
            continue
        routes = getattr(app, "routes", None) or []
        if any(getattr(route, "name", "") == "static-assets" for route in routes):
            return app
    return None


def ensure_download_route() -> None:
    """Attach /rbws-download/{token} ahead of Streamlit's page handler.

    Streamlit mounts the whole site at '/'. A route added after that mount
    never runs, so the download link opens the app in a new tab.
    """
    global _ROUTE_READY
    if _ROUTE_READY:
        return
    app = _streamlit_server_app()
    if app is None:
        return
    routes = app.router.routes
    existing = next((route for route in routes if getattr(route, "path", "") == _ROUTE_PATH), None)
    if existing is not None:
        if routes[0] is not existing:
            routes.remove(existing)
            routes.insert(0, existing)
        _ROUTE_READY = True
        return
    routes.insert(0, Route(_ROUTE_PATH, _download, methods=["GET", "HEAD"], name="rbws-download"))
    _ROUTE_READY = True


def _trim_old_workbooks(keep: int = 3) -> None:
    files = sorted(DOWNLOAD_DIR.glob("*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[keep:]:
        old.unlink(missing_ok=True)
        old.with_suffix(".name").unlink(missing_ok=True)
