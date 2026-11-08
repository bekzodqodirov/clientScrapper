"""``directories`` — the Uzbek business directories (03 §2.6, T-22).

The schedulable unit is the parent ``sources`` row ``directories`` (family
``directory``): its cron, its page budget (600/day, shared by every site) and
its pause are the runner's. Under it, one sub-source per site (``SUBSOURCES``)
with one parser module each — **only where a recording exists**:

* ``goldenpages`` (``directories_goldenpages``), ``yellowpages``
  (``directories_yellowpages``), ``glotr`` (``directories_glotr``) and
  ``topuz`` (``directories_topuz``, 0004: credited to the parent at first, so a
  weak top.uz's precision pause stopped every site — the T-22 review) —
  built, each pinned by its fixtures;
* ``prom`` (``directories_prom``: Cloudflare refused the recording),
  ``uzts``, ``openinfo`` and ``uzpharm`` (not reachable from the recording
  sandbox) — **registered and never run** (``spec`` None, the reason in
  ``owed``): no parser is written for a page nobody has seen.

**The fan-out.** A site runs when it has a parser and, when it has a
``sources`` row of its own, that row is ``enabled`` and not paused
(``paused_until > now``) — the precision job's week-long pause lands on the
attribution row a lead is credited to (05 §B.2, 06 Open question 23 (a)),
and ``/manbalar``'s switch on that row is the owner's per-site switch (the
rows are seeded off: each site is turned on after its day-1 check). A
skipped site is named in the run's ``meta.subsources`` and in the end
heartbeat's detail («detail names the sub-source», 03 §3).

**Two cadences** (03 §2.6, §3; the worker's ``JOB_TABLE``):

* the weekly **diff** — Thursday 23:00, the row's ``cron``: the first list
  page of every seed of every running site, and a card only for a company the
  source has never observed;
* the monthly **full crawl** — 23:00 on the 1st–7th, variant ``full``: one
  site per night (``SubSource.full_night``), every list page the site's
  verified pagination reaches (``SiteSpec.paginated``: goldenpages
  ``&Page=N``, top.uz ``?PAGEN_1=N``; yellowpages and glotr page 1 only — the
  one unverified, the other refused by robots.txt), and a card for every
  company not observed in the last ``CARD_FRESH`` days, so a site larger than
  one night's budget is walked whole over two months.

**The night both fire** (``plan``; the T-22 review). On the Thursday of the
1st–7th the two jobs start the same minute and the second is refused
``already_running``; whichever claims the run does both: tonight's site in
full, every other site by its diff — the diff is due when the parent row's
own cron fires that Tashkent day (``diff_due``), so a cron the owner moves
moves the rule with it. The first version ran tonight's full crawl alone, so
three sites lost their week, and nothing at all was crawled on the nights
that belong to unbuilt sites.

**A list page → cards.** ``SiteSpec.parse_list`` gives the companies of a
page (``common.Entry``), the next page as printed, and the organiser's
figures. The paginator stops on an empty page, a first card id the seed has
already shown (03 §2.6 «paginator loops»), a page past ``last_page``, a
final URL that dropped the page number (``redirect_stop``: goldenpages), or
``MAX_LIST_PAGES``. Each company is handled once per run (glotr lists a
seller once per product). A card is fetched per company — the card is that
company's own evidence, whose excerpt a lead card's quote must be found in
(02 §2.6) — except where the list row is complete (yellowpages and top.uz
payload rows) and the row is held by the category filter or a competitor's:
the row is then the observation, and no request is spent. A card the site
does not answer (a 404, glotr's soft 404, robots) falls back to the list
row's own observation where it has one. A kept list row is its own evidence
(``own_evidence``: the runner's canonical body, filed at the page's URL with
the row in the fragment, ``row_url``), never the page's: an evidence row has
one excerpt, and a lead card's quote must be found in the row's own words.
A stop on a budget, a deadline or a breaker keeps what was found, the list
rows of the cards not reached included.

**One bad request costs that request** (the T-22 review): a list page or a
card that fails in transport once the Fetcher's retries are spent
(``TRANSPORT_FAILURES``), or that the Fetcher refuses for a reason of its own
(``FetchError``), costs that seed or that card — a card's list row is kept —
and is counted (``list_status.transport``, ``card_errors``); a page its
parser cannot read likewise (``parse_errors``, logged by type). Only
``TRANSPORT_STREAK`` transport failures in a row on one site end that site's
night (``stops.transport``, its rows kept): the host stopped answering, and the
other sites still run.

Requests go through the run's ``Fetcher`` alone (robots.txt per host, the
per-host pace — 0.3 req/s for these hosts, under 03 §2.6's 0.5 rps; glotr's
``Crawl-delay: 10`` wins — the breaker, the page budget). The run's session
is never used for a read while the crawl runs (a 15-minute
``idle_in_transaction_session_timeout`` would kill a long crawl's connection):
lookups open a short session of their own.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal

import sqlalchemy as sa

from clientscrapper.collectors.base import (
    Budget,
    Collector,
    Observation,
    own_evidence,
    row_url,
)
from clientscrapper.collectors.directories import glotr, goldenpages, topuz, yellowpages
from clientscrapper.collectors.directories.common import Entry, ListPage, SiteSpec
from clientscrapper.config import settings
from clientscrapper.db.models import Observation as ObservationRow
from clientscrapper.db.models import Source
from clientscrapper.db.session import SessionLocal
from clientscrapper.services import crontab
from clientscrapper.services.http import (
    TRANSPORT_FAILURES,
    BudgetExhausted,
    FetchError,
    FetchResult,
    HostPaused,
    RobotsDisallowed,
    url_id,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

PARENT: Final = "directories"
Mode = Literal["diff", "full"]
FULL: Final = "full"
VARIANTS: Final = (None, FULL)
# 03 §2.6: «one sub-source per night during the monthly full crawl (1st–7th)».
FULL_NIGHTS: Final = range(1, 8)
# A seed's list pages in one full crawl (top.uz's 815-company section is 41).
MAX_LIST_PAGES: Final = 60
# A full crawl leaves alone a company observed this recently.
CARD_FRESH: Final = timedelta(days=45)
# Transport failures in a row that end one site's night: its host stopped
# answering (the other sites still run).
TRANSPORT_STREAK: Final = 5
# What one request can raise that costs that request only; BudgetExhausted
# and HostPaused (a breaker) stop the run and are caught before these.
REQUEST_FAILURES: Final = (*TRANSPORT_FAILURES, FetchError)
OWED_PROM: Final = (
    "refused by the site: Cloudflare 403 on robots.txt and /tags/optom/ "
    "(2026-10-29) — re-check from the VPS"
)
OWED_UNREACHED: Final = (
    "not recorded: the host was outside the recording sandbox's allowlist "
    "(2026-10-29) — record a list page and a card on day 1"
)


@dataclass(frozen=True, slots=True)
class SubSource:
    """One directory site. ``source_key`` is the attribution row its
    evidence and observations carry (``directories`` itself for a site with no
    row); ``full_night`` the day of the month of its full crawl; ``spec`` its
    parsers, None for a site that is registered and not built (``owed``)."""

    site: str
    source_key: str
    host: str
    full_night: int
    spec: SiteSpec | None
    owed: str | None = None


SUBSOURCES: Final[tuple[SubSource, ...]] = (
    *(
        SubSource(spec.site, spec.source_key, spec.host, night, spec)
        for night, spec in enumerate(
            (goldenpages.SPEC, yellowpages.SPEC, topuz.SPEC, glotr.SPEC), start=1
        )
    ),
    SubSource("prom", "directories_prom", "prom.uz", 5, None, OWED_PROM),
    SubSource("uzts", PARENT, "uzts.uz", 6, None, OWED_UNREACHED),
    SubSource("openinfo", PARENT, "openinfo.uz", 7, None, OWED_UNREACHED),
    SubSource("uzpharm", PARENT, "uzpharm-control.uz", 7, None, OWED_UNREACHED),
)


def tonight(now: datetime) -> tuple[SubSource, ...]:
    """The sites whose full crawl is tonight (Tashkent day of the month)."""
    day = now.astimezone(settings.tz).day
    return tuple(sub for sub in SUBSOURCES if sub.full_night == day)


def diff_due(cron: str | None, now: datetime) -> bool:
    """The weekly diff fires on ``now``'s Tashkent day: the parent row's own
    cron (``sources.directories.cron``), read as the scheduler reads it."""
    if not cron:
        return False
    local = now.astimezone(settings.tz)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        fired = crontab.fires(
            cron,
            after=start - timedelta(seconds=1),
            until=start + timedelta(days=1),
            tz=settings.tz,
            limit=1,
        )
        return next(fired, None) is not None
    except crontab.CrontabError:
        return False


def plan(
    variant: str | None, now: datetime, *, diff_due: bool
) -> tuple[tuple[SubSource, Mode], ...]:
    """The run's sites and the cadence of each (the module docstring's «the
    night both fire»): tonight's site in full on a full night — the ``full``
    variant's, and the diff's when it fires on the 1st–7th — and every other
    site by its diff when the diff fired (variant None) or is due tonight."""
    if variant not in VARIANTS:
        raise ValueError(f"directories has no variant {variant!r}")
    full_night = variant == FULL or now.astimezone(settings.tz).day in FULL_NIGHTS
    full = tonight(now) if full_night else ()
    chosen: list[tuple[SubSource, Mode]] = [(sub, "full") for sub in full]
    if variant is None or diff_due:
        chosen += [(sub, "diff") for sub in SUBSOURCES if sub not in full]
    return tuple(chosen)


@dataclass(frozen=True, slots=True)
class RowState:
    """What the fan-out reads of a sub-source's own ``sources`` row."""

    enabled: bool
    paused_until: datetime | None


def skip_reason(sub: SubSource, row: RowState | None, now: datetime) -> str | None:
    """Why ``sub`` does not run now (None: it runs). A site with no row of its
    own runs under the parent row the runner already read."""
    if sub.spec is None:
        return "not_built"
    if sub.source_key == PARENT:
        return None
    if row is None:
        return "no_sources_row"
    if not row.enabled:
        return "disabled"
    if row.paused_until is not None and row.paused_until > now:
        return "paused"
    return None


def seeds_for(sub: SubSource, config: Mapping[str, Any] | None) -> tuple[str, ...]:
    """The site's seeds: ``sources.directories.config.seeds.<site>`` when set
    (only https URLs on the site's own host), else the module's."""
    assert sub.spec is not None
    chosen = (config or {}).get("seeds")
    given = chosen.get(sub.site) if isinstance(chosen, Mapping) else None
    if not isinstance(given, list):
        return sub.spec.seeds
    prefix = f"https://{sub.host}/"
    return tuple(u for u in given if isinstance(u, str) and u.startswith(prefix))


def stop_reason(
    page: ListPage, *, spec: SiteSpec, asked: str, final: str, firsts: set[str]
) -> str | None:
    """Why the paginator stops at this page (None: the page is new)."""
    redirected = spec.redirect_stop(asked, final)
    if redirected is not None:
        return redirected
    if not page.entries:
        return "empty"
    if page.entries[0].ref in firsts:
        return "repeat"
    at, last = page.page, page.last_page
    if at is not None and last is not None and at > last:
        return "past_last"
    return None


def usable(result: FetchResult) -> bool:
    """A page to parse: a 200, or a 304 answered with the stored copy — never
    a challenge page (the breaker counts it as a 403)."""
    return (result.status == 200 or result.not_modified) and not result.captcha


def answer_of(result: FetchResult) -> str:
    return "captcha" if result.captcha else str(result.status)


def needs_card(entry: Entry) -> bool:
    """A card is asked for unless the list row is complete and settles the
    company already (held by the category filter, or a competitor's)."""
    if entry.card_url is None:
        return False
    obs = entry.observation
    if entry.complete and obs is not None:
        return not (obs.attrs.get("hold") or "competitor" in obs.signal_types)
    return True


async def known_refs(
    source_key: str, refs: Iterable[str], *, since: datetime | None
) -> set[str]:
    """The refs ``source_key`` has an observation of (since ``since``; ever
    when None). A short session of its own (see the module docstring)."""
    wanted = list(dict.fromkeys(refs))
    if not wanted:
        return set()
    ref = ObservationRow.parsed["source_ref"].astext
    stmt = sa.select(ref).where(ObservationRow.source_key == source_key, ref.in_(wanted))
    if since is not None:
        stmt = stmt.where(ObservationRow.created_at >= since)
    async with SessionLocal() as own:
        return set(await own.scalars(stmt.distinct()))


@dataclass(frozen=True, slots=True)
class Rows:
    """What the fan-out reads of ``sources``: each sub-source's own row, and
    the parent's ``config`` and ``cron``."""

    states: dict[str, RowState]
    config: Mapping[str, Any]
    cron: str | None


async def sub_rows() -> Rows:
    keys = {sub.source_key for sub in SUBSOURCES} | {PARENT}
    async with SessionLocal() as own:
        rows = (await own.scalars(sa.select(Source).where(Source.key.in_(keys)))).all()
    states = {r.key: RowState(bool(r.enabled), r.paused_until) for r in rows}
    parent = next((r for r in rows if r.key == PARENT), None)
    given = parent.config if parent is not None else None
    return Rows(
        states,
        given if isinstance(given, dict) else {},
        parent.cron if parent is not None else None,
    )


def _report() -> dict[str, Any]:
    return {
        "seeds": 0,
        "pages": 0,
        "cards": 0,
        "rows": 0,
        "held_rows": 0,
        "known": 0,
        "card_missing": 0,
        "card_errors": 0,
        "parse_errors": 0,
        "robots": 0,
        "stops": Counter[str](),
        "list_status": Counter[str](),
    }


class _SiteDown(Exception):
    """``TRANSPORT_STREAK`` transport failures in a row on one site."""


class Directories(Collector):
    key = "directories"
    family = "directory"
    default_cron = "0 23 * * 4"
    default_rps = 0.5
    default_page_budget = 600

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._ran: dict[str, str] = {}
        self._streak = 0

    def beat_detail(self) -> dict[str, Any]:
        return {"mode": self.run_meta.get("mode"), "subsources": dict(self._ran)}

    async def collect(self, session: AsyncSession, budget: Budget) -> list[Observation]:
        now = self.started_at or datetime.now(settings.tz)
        rows = await sub_rows()
        due = plan(self.variant, now, diff_due=diff_due(rows.cron, now))
        modes = sorted({mode for _, mode in due}, reverse=True)
        report: dict[str, dict[str, Any]] = {}
        self.run_meta["mode"] = "+".join(modes) or "none"
        self.run_meta["subsources"] = report
        if "full" in modes:
            self.run_meta["full_night"] = [sub.site for sub, m in due if m == "full"]
        seen: set[str] = set()
        for sub, mode in due:
            reason = skip_reason(sub, rows.states.get(sub.source_key), now)
            if reason is not None:
                report[sub.site] = {"skipped": reason}
                self._ran[sub.site] = reason
                continue
            assert sub.spec is not None
            stats = report[sub.site] = {"mode": mode, **_report()}
            self._ran[sub.site] = "ran"
            self._streak = 0
            try:
                for seed in seeds_for(sub, rows.config):
                    stats["seeds"] += 1
                    await self._seed(
                        sub.spec, seed, budget, mode=mode, now=now, stats=stats, seen=seen
                    )
            except _SiteDown:
                stats["stops"]["transport"] += 1
        return self.found

    def _failed(self, spec: SiteSpec, url: str, exc: Exception) -> None:
        """One request failed in transport: counted toward the site's streak."""
        log.warning(
            "directories: %s failed: %s",
            spec.site,
            type(exc).__name__,
            extra={"source": spec.source_key, "url_id": url_id(url)},
        )
        if isinstance(exc, TRANSPORT_FAILURES):
            self._streak += 1
            if self._streak >= TRANSPORT_STREAK:
                raise _SiteDown(spec.site)

    @staticmethod
    def _unreadable(
        spec: SiteSpec, url: str, exc: Exception, stats: dict[str, Any]
    ) -> None:
        stats["parse_errors"] += 1
        log.warning(
            "directories: %s page unreadable: %s",
            spec.site,
            type(exc).__name__,
            extra={"source": spec.source_key, "url_id": url_id(url)},
        )

    async def _seed(
        self,
        spec: SiteSpec,
        seed: str,
        budget: Budget,
        *,
        mode: Mode,
        now: datetime,
        stats: dict[str, Any],
        seen: set[str],
    ) -> None:
        """One seed's list pages: page 1, and in the full crawl the pages the
        site's pagination reaches."""
        url: str | None = seed
        firsts: set[str] = set()
        pages = 0
        while url is not None:
            try:
                result = await self.fetcher.get(
                    url, source=spec.source_key, budget=budget
                )
            except RobotsDisallowed:
                stats["robots"] += 1
                return
            except (BudgetExhausted, HostPaused):
                raise
            except REQUEST_FAILURES as exc:
                stats["list_status"]["transport"] += 1
                self._failed(spec, url, exc)
                return
            self._streak = 0
            if not usable(result):
                stats["list_status"][answer_of(result)] += 1
                return
            try:
                page = spec.parse_list(
                    result.body, url=result.final_url, fetched_at=result.fetched_at
                )
            except Exception as exc:
                self._unreadable(spec, url, exc, stats)
                return
            stop = stop_reason(
                page, spec=spec, asked=url, final=result.final_url, firsts=firsts
            )
            if stop is not None:
                stats["stops"][stop] += 1
                return
            firsts.add(page.entries[0].ref)
            pages += 1
            stats["pages"] += 1
            at = page.page or 1
            if not spec.paginated and page.last_page and at < page.last_page:
                # Pages the plan cannot reach (yellowpages: pagination unverified).
                stats["pages_owed"] = stats.get("pages_owed", 0) + page.last_page - at
            await self._entries(
                spec, page, budget, mode=mode, now=now, stats=stats, seen=seen
            )
            if mode != "full" or not spec.paginated:
                return
            if pages >= MAX_LIST_PAGES:
                stats["stops"]["page_cap"] += 1
                return
            url = page.next_url
        stats["stops"]["last"] += 1

    async def _entries(
        self,
        spec: SiteSpec,
        page: ListPage,
        budget: Budget,
        *,
        mode: Mode,
        now: datetime,
        stats: dict[str, Any],
        seen: set[str],
    ) -> None:
        fresh = await known_refs(
            spec.source_key,
            (e.ref for e in page.entries if e.ref not in seen),
            since=None if mode == "diff" else now - CARD_FRESH,
        )
        todo: list[Entry] = []
        for entry in page.entries:
            if entry.ref in seen:
                continue
            seen.add(entry.ref)
            if entry.ref in fresh:
                stats["known"] += 1
                continue
            todo.append(entry)
        for index, entry in enumerate(todo):
            try:
                await self._entry(spec, entry, budget, stats)
            except (BudgetExhausted, HostPaused, _SiteDown):
                # Nothing parsed is lost: the rows of the cards not reached.
                for rest in todo[index + 1 :]:
                    self._keep_row(rest, stats)
                raise

    async def _entry(
        self, spec: SiteSpec, entry: Entry, budget: Budget, stats: dict[str, Any]
    ) -> None:
        """One company of a list page: its card, or its list row."""
        if not needs_card(entry):
            self._keep_row(entry, stats)
            return
        assert entry.card_url is not None
        try:
            card = await self.fetcher.get(
                entry.card_url, source=spec.source_key, budget=budget
            )
        except (BudgetExhausted, HostPaused):
            self._keep_row(entry, stats)
            raise
        except RobotsDisallowed:
            stats["robots"] += 1
            self._keep_row(entry, stats)
            return
        except REQUEST_FAILURES as exc:
            stats["card_errors"] += 1
            self._keep_row(entry, stats)
            self._failed(spec, entry.card_url, exc)
            return
        self._streak = 0
        found: list[Observation] = []
        if usable(card):
            try:
                found = spec.parse_card(
                    card.body, url=card.final_url, fetched_at=card.fetched_at
                )
            except Exception as exc:
                self._unreadable(spec, entry.card_url, exc, stats)
        if found:
            stats["cards"] += 1
            self.found.extend(found)
        else:
            stats["card_missing"] += 1
            self._keep_row(entry, stats)

    def _keep_row(self, entry: Entry, stats: dict[str, Any]) -> None:
        if entry.observation is None:
            return
        self.found.append(own_evidence(entry.observation))
        stats["rows"] += 1
        if entry.observation.attrs.get("hold"):
            stats["held_rows"] += 1


__all__ = [
    "CARD_FRESH",
    "FULL_NIGHTS",
    "MAX_LIST_PAGES",
    "PARENT",
    "SUBSOURCES",
    "TRANSPORT_STREAK",
    "Directories",
    "SubSource",
    "diff_due",
    "known_refs",
    "needs_card",
    "own_evidence",
    "plan",
    "row_url",
    "seeds_for",
    "skip_reason",
    "stop_reason",
    "tonight",
]
