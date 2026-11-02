"""The workbook route must win over Streamlit's catch-all page."""

from starlette.applications import Starlette
from starlette.responses import HTMLResponse
from starlette.routing import Mount
from starlette.testclient import TestClient

import rbws_extractor.web_download as wd


class _Spa:
    async def __call__(self, scope, receive, send):
        await HTMLResponse("<html>streamlit</html>")(scope, receive, send)


def test_download_route_is_served_before_the_app_page(tmp_path, monkeypatch):
    monkeypatch.setattr(wd, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(wd, "_ROUTE_READY", False)
    src = tmp_path / "in.xlsx"
    src.write_bytes(b"PK\x03\x04workbook")
    app = Starlette(routes=[Mount("/", app=_Spa(), name="static-assets")])

    token = wd.publish_workbook(src, "ProcessPiping_RBWS_REVIEW.xlsx")
    client = TestClient(app)

    response = client.get(f"/rbws-download/{token}")
    assert response.status_code == 200
    assert response.content == b"PK\x03\x04workbook"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert "ProcessPiping_RBWS_REVIEW.xlsx" in disposition
    assert client.get("/").text == "<html>streamlit</html>"
    assert app.router.routes[0].path == "/rbws-download/{token}"
