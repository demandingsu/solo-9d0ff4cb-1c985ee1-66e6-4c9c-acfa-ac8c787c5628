"""Gap reconciliation: catalogue generation, pagination, archive snapshot."""
from __future__ import annotations

import sqlite3

from app.domain import SourceUnavailable
from app.reconcile import ReconcileService
from app.source import HttpSourceClient
from app.store import ArchiveStore
from demo.store import DemoStore

N1 = "ZFCG-2026-0001"
N2 = "ZFCG-2026-0002"


def _reconciler(store: ArchiveStore, demo, page_size: int = 20) -> ReconcileService:
    source = HttpSourceClient("http://demo.test", client=demo)
    return ReconcileService(store, source, page_size=page_size)


def test_catalog_generation_bumps_on_every_change(demo):
    store: DemoStore = demo.app.state.store
    g0 = store.catalog_generation()

    demo.post(f"/notice/{N1}/retract", data={"action": "retract"},
              follow_redirects=False)
    assert store.catalog_generation() == g0 + 1

    demo.post(f"/notice/{N1}/retract", data={"action": "restore"},
              follow_redirects=False)
    assert store.catalog_generation() == g0 + 2

    demo.post(
        f"/notice/{N1}/edit",
        data={"title": "改稿标题", "body": "改稿正文"},
        follow_redirects=False,
    )
    assert store.catalog_generation() == g0 + 3


def test_catalog_api_pagination_and_generation(demo):
    body = demo.get("/api/catalog?page=1&page_size=1").json()
    assert body["total"] == 2
    assert body["total_pages"] == 2
    assert body["page"] == 1
    assert len(body["items"]) == 1
    g = body["generation"]
    item = body["items"][0]
    assert set(item) == {"number", "title", "revision", "retracted", "updated_at"}

    page2 = demo.get("/api/catalog?page=2&page_size=1").json()
    assert page2["generation"] == g
    assert [i["number"] for i in page2["items"]] == [N2]

    bad = demo.get("/api/catalog?page=0&page_size=1")
    assert bad.status_code == 400
    bad = demo.get("/api/catalog?page=1&page_size=101")
    assert bad.status_code == 400


def test_reconcile_lists_missing_behind_and_retraction_mismatch(
    service, store, demo
):
    # Archive N1 at r1; leave N2 entirely unarchived.
    service.capture(N1)

    # Source N1 moves to r2 (body edit) and gets retracted on top; N2 stays r1.
    demo.post(
        f"/notice/{N1}/edit",
        data={"title": "市政道路照明设备采购招标公告", "body": "第二版正文"},
        follow_redirects=False,
    )
    demo.post(f"/notice/{N1}/retract", data={"action": "retract"},
              follow_redirects=False)

    report = _reconciler(store, demo).reconcile()
    assert report.completed is True
    assert report.catalog_total == 2
    assert report.archived_total == 1
    by_number = {e.number: e for e in report.entries}

    assert N1 in by_number and by_number[N1].kind == "behind"
    behind = by_number[N1]
    assert behind.source_revision == 2
    assert behind.archived_revision == 1
    assert behind.source_retracted is True  # source state still shown

    assert N2 in by_number and by_number[N2].kind == "missing"
    missing = by_number[N2]
    assert missing.archived_revision is None
    assert missing.archived_retracted is None
    assert missing.source_revision == 1

    # Kinds are ordered missing -> behind -> retraction_mismatch.
    kinds = [e.kind for e in report.entries]
    assert kinds == sorted(kinds, key={"missing": 0, "behind": 1,
                                       "retraction_mismatch": 2}.get)


def test_reconcile_clean_when_fully_archived(service, store, demo):
    service.capture(N1)
    service.capture(N2)
    report = _reconciler(store, demo).reconcile()
    assert report.completed is True
    assert report.entries == []
    assert report.catalog_total == 2


def test_reconcile_detects_retraction_mismatch_alone(service, store, demo):
    # Same revision archived, source retracts without a new revision.
    service.capture(N1)
    demo.post(f"/notice/{N1}/retract", data={"action": "retract"},
              follow_redirects=False)

    report = _reconciler(store, demo).reconcile()
    assert report.completed is True
    (entry,) = [e for e in report.entries if e.number == N1]
    assert entry.kind == "retraction_mismatch"
    assert entry.source_revision == entry.archived_revision == 1
    assert entry.source_retracted is True
    assert entry.archived_retracted is False


def test_reconcile_paginates_all_pages(service, store, demo):
    service.capture(N1)  # N2 stays missing, force page_size=1
    report = _reconciler(store, demo, page_size=1).reconcile()
    assert report.completed is True
    assert report.pages_fetched == 2
    assert report.page_size == 1
    numbers = sorted(e.number for e in report.entries)
    assert numbers == [N2]


def test_generation_change_between_pages_is_incomplete_with_no_partial_list(
    service, store, demo
):
    service.capture(N1)
    service.capture(N2)
    reconciler = _reconciler(store, demo, page_size=1)

    def mutate_after_first_page(page):
        if page.page == 1:
            # A new notice between page 1 and page 2 bumps the generation.
            demo.post(
                "/create",
                data={"number": "ZFCG-2026-0003", "title": "翻页中新增",
                      "body": "新增正文"},
                files={"attachment": ("a.txt", b"new-blob", "text/plain")},
                follow_redirects=False,
            )

    report = reconciler.reconcile(on_page_fetched=mutate_after_first_page)
    assert report.completed is False
    assert report.entries == []
    assert "目录代次" in report.reason
    # Read-only: nothing was archived by reconciliation.
    assert sorted(a["number"] for a in store.list_announcements()) == [N1, N2]


def test_edit_between_pages_is_incomplete(service, store, demo):
    service.capture(N1)
    service.capture(N2)
    reconciler = _reconciler(store, demo, page_size=1)

    def edit_after_first_page(page):
        if page.page == 1:
            demo.post(
                f"/notice/{N2}/edit",
                data={"title": "绿化公告", "body": "翻页中改稿"},
                follow_redirects=False,
            )

    report = reconciler.reconcile(on_page_fetched=edit_after_first_page)
    assert report.completed is False
    assert report.entries == []
    assert report.reason


def test_read_failure_marks_incomplete_and_keeps_archive(service, store, demo):
    service.capture(N1)
    reconciler = _reconciler(store, demo, page_size=1)

    real_fetch = reconciler.source.fetch_catalog_page
    calls = {"n": 0}

    def flaky(page, page_size):
        calls["n"] += 1
        if calls["n"] == 2:
            raise SourceUnavailable("模拟第二页连接中断；本次核对未改动档案。")
        return real_fetch(page, page_size)

    reconciler.source.fetch_catalog_page = flaky
    report = reconciler.reconcile()
    assert report.completed is False
    assert report.entries == []
    assert "第二页" in report.reason
    # The archive is untouched by the failed reconciliation.
    assert store.get_version(N1, 1) is not None
    assert len(store.list_announcements()) == 1


def test_interleaved_capture_during_pagination_uses_one_archive_snapshot(
    service, store, demo
):
    """Another window captures a new revision mid-pagination.

    The archive snapshot is pinned before pages are fetched, so every entry in
    the report must describe the same committed archive state (the pre-capture
    state), even though the committed database ends with the newer revision.
    """
    service.capture(N1)  # archived r1
    service.capture(N2)  # archived r1

    # Source advances N1 to r2 before the run; generation is already stable.
    demo.post(
        f"/notice/{N1}/edit",
        data={"title": "市政道路照明设备采购招标公告", "body": "第二版正文"},
        follow_redirects=False,
    )
    assert demo.get(f"/api/notices/{N1}").json()["revision"] == 2

    reconciler = _reconciler(store, demo, page_size=1)

    def concurrent_capture_after_first_page(page):
        if page.page == 1:
            # Simulate the other window committing r2 while we are paginating.
            result = service.capture(N1)
            assert result.revision == 2

    report = reconciler.reconcile(
        on_page_fetched=concurrent_capture_after_first_page
    )
    assert report.completed is True
    # The whole report reflects the pinned snapshot taken before pagination:
    # N1 still looks "behind" with archived r1 on every row that mentions it.
    n1 = next(e for e in report.entries if e.number == N1)
    assert n1.kind == "behind"
    assert n1.source_revision == 2
    assert n1.archived_revision == 1
    # Meanwhile the committed database already holds the interleaved capture.
    assert store.latest_version(N1).revision == 2
    # The reconciler never wrote anything itself.
    assert all(
        event["outcome"] != "reconcile"
        for event in store.recent_events(limit=100)
    )


def test_reconcile_is_read_only_on_storage_failure(service, store, demo):
    service.capture(N1)
    before_logs = len(store.recent_events(limit=500))

    class ExplodingConnection:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def execute(self, sql, *params):
            if sql.strip().upper().startswith("INSERT"):
                raise AssertionError("reconciliation must never INSERT")
            return self._conn.execute(sql, *params)

    real_connect = store._connect
    store._connect = lambda: ExplodingConnection(real_connect())
    try:
        report = _reconciler(store, demo).reconcile()
    finally:
        store._connect = real_connect
    assert report.completed is True
    assert len(store.recent_events(limit=500)) == before_logs


def test_storage_read_failure_is_reported_incomplete(service, store, demo):
    service.capture(N1)
    reconciler = _reconciler(store, demo)
    real_connect = store._connect

    class FailingConnection:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def execute(self, sql, *params):
            if "announcement_states" not in sql and "FROM announcements" in sql:
                raise sqlite3.OperationalError("disk I/O error")
            return self._conn.execute(sql, *params)

    store._connect = lambda: FailingConnection(real_connect())
    try:
        report = reconciler.reconcile()
    finally:
        store._connect = real_connect
    assert report.completed is False
    assert report.entries == []
    assert report.reason


def test_duplicate_number_across_pages_is_incomplete(
    service, store, demo, monkeypatch
):
    service.capture(N1)
    service.capture(N2)
    reconciler = _reconciler(store, demo, page_size=1)
    real_fetch = reconciler.source.fetch_catalog_page

    def fake_fetch(page, page_size):
        result = real_fetch(page, page_size)
        if page == 2:
            # Tamper: repeat page 1's number on page 2.
            from app.domain import CatalogPage

            first_items = real_fetch(1, page_size).items
            return CatalogPage(
                page=result.page,
                page_size=result.page_size,
                total=result.total,
                total_pages=result.total_pages,
                generation=result.generation,
                items=first_items,
            )
        return result

    monkeypatch.setattr(reconciler.source, "fetch_catalog_page", fake_fetch)
    report = reconciler.reconcile()
    assert report.completed is False
    assert report.entries == []
    assert "重复" in report.reason
