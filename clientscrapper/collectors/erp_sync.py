"""``erp_sync`` — the ERP feeds on a schedule (03 §2.2, 03 §3).

A thin scheduler shell: every rule is ``services/erp_feeds``'s (05 §B.2). The
runner (``collectors/runner.py``) reads the ``sources.erp_sync`` row, skips a
disabled or paused one, claims the run — so a delta that fires while a full
pull runs is skipped as ``already_running`` (03 §2.2) — beats
``collector:erp_sync.<variant>`` and writes ``collector_runs`` with the feed's
report in ``meta.suppression``. While the feeds are not configured (the
«Boshqa» fallback, ``CRM_PUSH_SOURCE=other``) every run is skipped with
``feeds_not_configured`` and no feed is called (05 §B.3).

Five cadences — the suppression pulls (T-09) and the outcomes walks (T-10):

* ``delta`` — ``15 * * * *`` (the row's own ``cron``): additive; a new
  ``salt_id`` on its first page makes it a full pull at once, and so does a
  last full pull older than ``erp_feeds.FULL_DUE_AFTER`` (the catch-up of a
  failed or missed night);
* ``full`` — ``30 2 * * *``: the only walk that deletes;
* ``outcomes`` — ``0 3 * * *``: the ``cursor`` walk, then the ``recent`` one,
  then — both completed — the 48-hour rule (``reconcile_unconfirmed``), then
  the catch-up of a Sunday or monthly walk that has not completed for too
  long (``erp_feeds.CATCH_UP_AFTER``);
* ``quarter`` — ``20 3 * * 0`` (Sunday, before ``source_precision_job`` at
  04:00): the 90-day walk;
* ``year`` — ``40 3 1 * *``: every row of the year (the only read that sees
  a stage-kind flip on an old row).

The variants are the slots the scheduler's table names (worker/main.py,
T-08): ``JOBS`` is what it registers — id, cron and variant per cadence;
each runs ``run_collector(ErpSync, variant=…)``, which beats
``collector:erp_sync.<variant>``. The runner's one-running claim keeps two
of them from overlapping (a walk that fires while another runs is skipped
``already_running``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, ClassVar, Final, Literal

from clientscrapper.collectors.base import Budget, Collector, Observation
from clientscrapper.collectors.runner import RunReport, SessionFactory, run_collector
from clientscrapper.config import settings
from clientscrapper.db.session import SessionLocal
from clientscrapper.services import erp_feeds

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from clientscrapper.services.http import Fetcher

Variant = Literal["full", "delta", "outcomes", "quarter", "year"]
VARIANTS: Final[tuple[Variant, ...]] = ("full", "delta", "outcomes", "quarter", "year")
NOT_CONFIGURED: Final = "feeds_not_configured"
# The walks each outcomes variant makes, in order (05 §B.2's table).
OUTCOME_WALKS: Final[dict[str, tuple[erp_feeds.Walk, ...]]] = {
    "outcomes": ("cursor", "recent"),
    "quarter": ("quarter",),
    "year": ("year",),
}


class ErpSync(Collector):
    key = "erp_sync"
    family = "erp"
    default_cron = "15 * * * *"
    default_rps = 1.0
    default_page_budget = 50
    # The owner's ``tools/erp_sync.py --full --accept-shrink`` (06 §4.6).
    accept_shrink: ClassVar[bool] = False

    @classmethod
    def unavailable(cls) -> str | None:
        return None if erp_feeds.feeds_configured() else NOT_CONFIGURED

    @classmethod
    def client(cls) -> erp_feeds.ErpClient:
        """A client on ``CRM_BASE_URL`` with ``CRM_AGENT_KEY``; ``bound``
        hands one in instead."""
        return erp_feeds.client_from_settings()

    @classmethod
    def owns_client(cls) -> bool:
        return True

    async def collect(self, session: AsyncSession, budget: Budget) -> list[Observation]:
        variant = self.variant
        if variant not in VARIANTS:
            raise ValueError(f"erp_sync has no variant {variant!r}")
        now = self.started_at or datetime.now(settings.tz)
        erp = self.client()
        try:
            if variant in OUTCOME_WALKS:
                await self._outcomes(session, erp, OUTCOME_WALKS[variant], now)
                return []
            mode: erp_feeds.SyncMode = "full" if variant == "full" else "delta"
            report = await erp_feeds.sync_suppression(
                session, erp, mode=mode, now=now, accept_shrink=self.accept_shrink
            )
            self.run_meta["suppression"] = report.as_meta()
            if report.status == "salt_changed":
                # 05 §B.2 step 2: the key was re-minted — a full pull now,
                # in this run, so the gate is not held for a day.
                full = await erp_feeds.sync_suppression(
                    session, erp, mode="full", now=now, accept_shrink=self.accept_shrink
                )
                self.run_meta["suppression_full"] = full.as_meta()
        except erp_feeds.FeedError as exc:
            self.run_meta["feed_error"] = failure_note(exc)
            raise
        finally:
            if self.owns_client():
                await erp.aclose()
        # The feeds write suppression rows, never observations (02 §2.5).
        return []

    async def _outcomes(
        self,
        session: AsyncSession,
        erp: erp_feeds.ErpClient,
        walks: tuple[erp_feeds.Walk, ...],
        now: datetime,
    ) -> None:
        """The walks in order (a failed one stops the run), their reports in
        ``meta.outcomes``; after the nightly pair, the 48-hour rule, then the
        overdue Sunday / monthly walks (after the rule, so a catch-up that
        fails never holds it)."""
        done: dict[str, object] = {}
        self.run_meta["outcomes"] = done
        for walk in walks:
            report = await erp_feeds.sync_outcomes(session, erp, walk=walk, now=now)
            done[walk] = report.as_meta()
        if walks != OUTCOME_WALKS["outcomes"]:
            return
        reconcile = await erp_feeds.reconcile_unconfirmed(session, erp, now=now)
        self.run_meta["reconcile"] = reconcile.as_meta()
        late = (await erp_feeds.outcomes_config(session)).catch_up(now)
        if late:
            self.run_meta["catch_up"] = list(late)
        for walk in late:
            report = await erp_feeds.sync_outcomes(session, erp, walk=walk, now=now)
            done[walk] = report.as_meta()


def failure_note(exc: erp_feeds.FeedError) -> dict[str, object]:
    note: dict[str, object] = {"error": type(exc).__name__}
    if isinstance(exc, erp_feeds.ErpHttpError):
        note["status"] = exc.status
    code = erp_feeds.contract_code(exc)
    if code is not None:
        note["code"] = code
    return note


def bound(
    erp: erp_feeds.ErpClient | None, *, accept_shrink: bool = False
) -> type[ErpSync]:
    """``ErpSync`` reading through ``erp`` (the caller closes it), or through
    its own client when None; with ``accept_shrink`` for one full pull."""
    shrink = accept_shrink

    class Bound(ErpSync):
        accept_shrink = shrink

        @classmethod
        def client(cls) -> erp_feeds.ErpClient:
            return erp if erp is not None else erp_feeds.client_from_settings()

        @classmethod
        def owns_client(cls) -> bool:
            return erp is None

    return Bound


@dataclass(frozen=True, slots=True)
class ErpJob:
    """One cadence for the scheduler: ``job:<id>``, a crontab (Tashkent) and
    the variant the run beats as ``collector:erp_sync.<variant>``."""

    id: str
    cron: str
    variant: Variant
    task: str = "T-09"


# 03 §3: the delta is the row's own cron (sources.cron); the full pull runs
# after the Yandex night run (02:10) and before the outcomes walks (03:00);
# the Sunday walk before source_precision_job (04:00), the monthly one after
# both. Ids and variants are the scheduler table's slots (worker/main.py).
JOBS: Final = (
    ErpJob("erp_sync_delta", "15 * * * *", "delta"),
    ErpJob("erp_sync_full", "30 2 * * *", "full"),
    ErpJob("erp_sync_outcomes", "0 3 * * *", "outcomes", "T-10"),
    ErpJob("erp_sync_quarter", "20 3 * * 0", "quarter", "T-10"),
    ErpJob("erp_sync_year", "40 3 1 * *", "year", "T-10"),
)


async def run(
    variant: Variant,
    *,
    fetcher: Fetcher,
    now: datetime | None = None,
    erp: erp_feeds.ErpClient | None = None,
    accept_shrink: bool = False,
    session_factory: SessionFactory = SessionLocal,
) -> RunReport:
    """One scheduled (or hand-started) run of ``variant``. ``fetcher`` is the
    worker's shared one — the runner binds it to the run; the feeds go
    through ``ErpClient``, not through it."""
    cls = bound(erp, accept_shrink=accept_shrink) if erp or accept_shrink else ErpSync
    return await run_collector(
        cls,
        fetcher=fetcher,
        now=now or datetime.now(settings.tz),
        variant=variant,
        session_factory=session_factory,
    )
