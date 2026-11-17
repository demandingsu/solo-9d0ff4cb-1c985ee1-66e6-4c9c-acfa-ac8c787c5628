"""Demo catalogue API and browser pagination behaviour."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app as create_archive_app
from demo.main import create_app


def test_demo_browser_index_paginates_and_shows_generation(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_CATALOG_PAGE_SIZE", "1")
    app = create_app(str(tmp_path / "demo-paged.db"))
    with TestClient(app) as paged:
        page = paged.get("/?page=1").text
        assert "目录代次" in page
        assert "第 1 / 2 页" in page
        assert "下一页" in page
        assert "ZFCG-2026-0001" in page

        page2 = paged.get("/?page=2").text
        assert "第 2 / 2 页" in page2
        assert "ZFCG-2026-0002" in page2


def test_catalog_generation_moves_after_create(demo):
    before = demo.get("/api/catalog").json()["generation"]
    demo.post(
        "/create",
        data={"number": "ZFCG-2026-0099", "title": "新公告", "body": "正文"},
        files={"attachment": ("a.txt", b"blob", "text/plain")},
        follow_redirects=False,
    )
    after = demo.get("/api/catalog").json()
    assert after["generation"] == before + 1
    assert after["total"] == 3


def test_archive_reconcile_runs_against_demo_catalog(tmp_path, demo):
    app = create_archive_app(
        db_path=str(tmp_path / "a.db"),
        source_base_url="http://demo.test",
        source_client=demo,
    )
    with TestClient(app) as client:
        report_page = client.post("/reconcile")
        assert report_page.status_code == 200
        assert "核对完成" in report_page.text
