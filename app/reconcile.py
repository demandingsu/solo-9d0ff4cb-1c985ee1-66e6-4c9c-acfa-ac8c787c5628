"""Gap reconciliation: source catalogue vs. one committed archive snapshot.

The archivist triggers this before bid closing to find notices that exist on
the source site but are missing, behind, or have a mismatched withdrawal flag
in the archive.

Two consistency guarantees shape the whole flow:

1. Same catalogue generation. The source tags every catalogue page with a
   monotonic generation that bumps on every create/edit/retraction. Pages are
   fetched one at a time; if a later page carries a different generation or a
   different total/total_pages than the first page, the source changed while
   we were paginating. The run is then reported as *incomplete* and no partial
   discrepancy list is shown.

2. Same committed archive state. Before any HTTP page is fetched, a single
   read-only transaction pins an archive snapshot (WAL snapshot isolation).
   A capture committing in another window during pagination cannot make
   individual comparisons see different archive states. The reconciliation is
   strictly read-only: failures never modify the archive.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from .domain import (
    ArchiveError,
    CatalogChanged,
    CatalogPage,
    ReconcileEntry,
    ReconcileReport,
    SourceUnavailable,
    utc_now_iso,
)
from .source import HttpSourceClient
from .store import ArchiveStore

log = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 20
MAX_PAGES = 1000


class ReconcileService:
    def __init__(
        self,
        store: ArchiveStore,
        source: HttpSourceClient,
        *,
        page_size: int = DEFAULT_PAGE_SIZE,
    ):
        if not 1 <= page_size <= 100:
            raise ValueError("page_size 必须在 1–100 之间")
        self.store = store
        self.source = source
        self.page_size = page_size

    def reconcile(
        self,
        *,
        on_page_fetched: Callable[[CatalogPage], None] | None = None,
    ) -> ReconcileReport:
        """Run a full catalogue reconciliation.

        ``on_page_fetched`` is invoked after every successfully fetched page
        (before the next request) and exists mainly so tests can mutate the
        source between pages to prove the generation guard.
        """
        started_at = utc_now_iso()
        try:
            return self._run(started_at, on_page_fetched)
        except ArchiveError as exc:
            # Source or storage failure: the run is incomplete, never partial.
            log.info("reconciliation incomplete: %s", exc.message)
            return ReconcileReport(
                completed=False,
                catalog_generation=0,
                catalog_total=0,
                pages_fetched=0,
                page_size=self.page_size,
                reason=exc.message,
                archived_total=0,
                started_at=started_at,
                finished_at=utc_now_iso(),
            )

    def _run(
        self,
        started_at: str,
        on_page_fetched: Callable[[CatalogPage], None] | None,
    ) -> ReconcileReport:
        # 1) Pin the archive state first; every comparison below uses it.
        # The read transaction stays open across the HTTP pagination so that
        # the whole result corresponds to one committed archive state.
        with self.store.snapshot() as view:
            archived = view.announcement_states()
            archived_total = len(archived)

            # 2) Page through the source catalogue, validating one generation.
            pages = self._fetch_all_pages(on_page_fetched)

            # 3) Compare while still inside the pinned archive snapshot.
            generation = pages[0].generation if pages else 0
            catalog_total = pages[0].total if pages else 0
            entries = self._build_entries(pages, archived)

        return ReconcileReport(
            completed=True,
            catalog_generation=generation,
            catalog_total=catalog_total,
            pages_fetched=len(pages),
            page_size=self.page_size,
            entries=entries,
            archived_total=archived_total,
            started_at=started_at,
            finished_at=utc_now_iso(),
        )

    # ------------------------------------------------------------- crawling

    def _fetch_all_pages(
        self,
        on_page_fetched: Callable[[CatalogPage], None] | None,
    ) -> list[CatalogPage]:
        pages: list[CatalogPage] = []
        first: CatalogPage | None = None
        page_no = 1
        while True:
            if page_no > MAX_PAGES:
                raise SourceUnavailable(
                    f"目录页数超过上限 {MAX_PAGES}，已停止核对；本次核对未改动档案。"
                )
            page = self.source.fetch_catalog_page(page_no, self.page_size)
            if first is None:
                first = page
            else:
                self._assert_same_generation(first, page)

            # Globally unique notice numbers: duplicates across pages would
            # mean the catalogue shifted under us even if the tag stayed same.
            pages.append(page)
            if on_page_fetched is not None:
                on_page_fetched(page)

            if page_no >= page.total_pages:
                break
            page_no += 1

        self._assert_no_duplicate_numbers(pages)
        return pages

    @staticmethod
    def _assert_same_generation(first: CatalogPage, page: CatalogPage) -> None:
        if (
            page.generation != first.generation
            or page.total != first.total
            or page.total_pages != first.total_pages
        ):
            raise CatalogChanged(
                "源站目录在翻页过程中发生变化（新增公告、改稿或撤稿导致目录代次"
                f"由 g{first.generation} 变为 g{page.generation}），"
                "本次核对未完成，不展示部分清单；请重新发起核对。"
            )

    @staticmethod
    def _assert_no_duplicate_numbers(pages: list[CatalogPage]) -> None:
        seen: set[str] = set()
        for page in pages:
            for item in page.items:
                if item.number in seen:
                    raise CatalogChanged(
                        f"源站目录在翻页过程中发生变化：公告号 {item.number} "
                        "在多页重复出现，本次核对未完成，不展示部分清单；"
                        "请重新发起核对。"
                    )
                seen.add(item.number)
        total_items = sum(len(p.items) for p in pages)
        first = pages[0]
        if total_items != first.total:
            raise CatalogChanged(
                "源站目录在翻页过程中发生变化：实际条目数与目录声明总数不一致，"
                "本次核对未完成，不展示部分清单；请重新发起核对。"
            )

    # ------------------------------------------------------------ comparing

    @staticmethod
    def _build_entries(
        pages: list[CatalogPage],
        archived: dict,
    ) -> list[ReconcileEntry]:
        kind_order = {"missing": 0, "behind": 1, "retraction_mismatch": 2}
        entries: list[ReconcileEntry] = []
        for page in pages:
            for item in page.items:
                state = archived.get(item.number)
                if state is None:
                    kind = "missing"
                    archived_rev = None
                    archived_retracted = None
                else:
                    if item.revision > state.latest_revision:
                        kind = "behind"
                    elif item.retracted != state.source_retracted:
                        kind = "retraction_mismatch"
                    else:
                        continue
                    archived_rev = state.latest_revision
                    archived_retracted = state.source_retracted

                entries.append(
                    ReconcileEntry(
                        kind=kind,
                        number=item.number,
                        title=item.title,
                        source_revision=item.revision,
                        source_retracted=item.retracted,
                        archived_revision=archived_rev,
                        archived_retracted=archived_retracted,
                    )
                )
        entries.sort(key=lambda e: (kind_order[e.kind], e.number))
        return entries
