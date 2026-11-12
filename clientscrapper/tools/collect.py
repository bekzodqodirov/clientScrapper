"""Run one collector now — the run its cron would make (06 §3.3).

    make collect SOURCE=directories|exhibitors [FULL=1]
    python -m clientscrapper.worker.main collect <slot> [<variant>]

``SOURCE`` is a slot of the worker's ``JOB_TABLE`` (``worker/main.py``) whose
collector is built — the slot's ``job`` is set and it names a ``sources``
row (``JobSlot``: «``source_key``/``variant`` name a collector run») — and
that the worker runs for the owner: not the Telegram reader's (the userbot
service reads the chats) and not the ERP feeds' (``make erp-sync``). The set
is read from the table (:func:`allowed`), so a collector a later task
builds (hh, OLX, the maps …) is offered the day its slot is filled, and a
slot still empty is «not built yet».

The run is the slot's own ``job`` — what ``build_scheduler`` registers,
``collector_job(<Collector>, <variant>)`` — run once in this one-off process
(``worker.collect_now``): the same ``run_collector`` with the shared Fetcher
(robots.txt, the per-host pace, the 403/429 breaker, the daily page budget,
``costs.ask`` before any paid call), the same evidence store and the same
``collector_runs`` row. The worker that leads keeps its scheduler and its
leader lock: nothing here takes or releases the lock, reclaims a run or
catches a job up; the runner's one-running claim keeps the two processes
from running one source at once (``already_running``). The one-off holds
its source's ``COLLECT_LOCK`` for the run's life, so a worker that starts or
resumes meanwhile (``make reload``, ``make update``, a crash restart) leaves
its ``running`` row alone instead of closing it as a dead worker's; a second
``make collect`` of the same source finds the lock held and runs nothing.

Before the run, the runner's own reasons not to run are asked
(``runner.skip_reason``: COLLECTORS_ENABLED, ``/toxtat``, the row switched
off, a pause), and for the directories the fan-out's per-site rule
(``directories.skip_reason`` over ``directories.plan``: the parent row alone
crawls nothing), then the day's page budget (``runner.build_budget``, read
only: a source with no page left today crawls nothing). A refusal is one
Uzbek sentence and exit 1, and writes nothing — except the one the runner
meets itself (the scheduler's run claimed the source first), which writes
the runner's usual ``skipped`` row, as a scheduled overlap would.

After it, the summary is the run's own ``collector_runs`` row — its counters
and its ``meta`` as the runner wrote them (per site for the directories,
``meta.subsources`` and ``meta.sources``; per list for the exhibitors,
``meta.lists``) — never a count of its own. The companies it found are
resolved, scored and carded by the running worker on its own cadences;
cards reach the bot inside REVIEW_HOURS (Mon–Sat), at most REVIEW_DAILY_CAP
a day.

``FULL=1`` (the ``full`` variant) is refused today (``NO_MANUAL``): the
directories' full crawl picks its site by the night (``SubSource.
full_night``: one site a night, the 1st–7th), so on the 8th–31st
``plan("full", …)`` crawls nothing in full, and on its own night the 23:00
job crawls that site anyway. Choosing the site by hand is later work
(docs/OPS.md). A plain run on the 1st–7th is ``plan(None, …)`` — the night's
site in full beside the others' diff, the run the Thursday diff makes on such
a day — and it says so before the crawl (:data:`TEXT` ``full_today``): it
spends the same day's page budget the 23:00 full crawl then reads with. A
variant slot is run by hand only when named in :data:`MANUAL_VARIANTS` (none
today): a later task's variant is judged before it is offered.

Exit codes: 0 the run finished (``ok`` or ``partial``, new companies or
none), 1 refused (nothing ran), 2 the run failed or was stopped, the database
did not answer, or the tool itself failed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any, Final

import psycopg
import sqlalchemy as sa

from clientscrapper.collectors import directories, runner
from clientscrapper.collectors.erp_sync import ErpSync
from clientscrapper.collectors.telegram_monitor import TelegramMonitor
from clientscrapper.config import settings
from clientscrapper.db.models import CollectorRun, Source
from clientscrapper.db.session import SessionLocal, engine
from clientscrapper.services import erp_feeds
from clientscrapper.services.http import BLOCKED_ERROR, BREAKER_PAUSE
from clientscrapper.services.lifecycle import install_stop_signals, remove_stop_signals
from clientscrapper.worker import main as worker
from clientscrapper.worker.main import JobSlot

log = logging.getLogger(__name__)

EXIT_DONE: Final = 0
EXIT_REFUSED: Final = 1
EXIT_FAILED: Final = 2

FULL: Final = "full"

# The owner's words (Uzbek Latin, tests/test_uz_text.py). Not bot/texts' UZ:
# this tool prints them in a terminal, and its keys are its own.
TEXT: Final[dict[str, str]] = {
    # --- refusals: one sentence, exit 1, nothing ran -------------------------------
    "usage": (
        "Qaysi manbani yigʻay? make collect SOURCE=<manba> deb yozing. "
        "Hozir mumkin: {allowed}."
    ),
    "unknown": "«{key}» degan manba yoʻq. Hozir mumkin: {allowed}.",
    "not_built": (
        "{key} hali qurilmagan — bu manbadan yigʻish keyinroq quriladi. "
        "Hozir mumkin: {allowed}."
    ),
    "telegram": (
        "Telegram guruhlarini userbot oʻqiydi, make collect emas: u doim oʻzi "
        "ishlab turadi (chatlar: botda /chats)."
    ),
    "erp": "ERP roʻyxatlari make erp-sync bilan yangilanadi, make collect emas.",
    "no_variant": (
        "{key} uchun «{variant}» yurishi yoʻq — FULL=1 ni qoʻymasdan ishga "
        "tushiring: make collect SOURCE={key}."
    ),
    "full_refused": (
        "FULL=1 hozircha yoʻq: kataloglarning toʻliq yurishini worker oʻzi qiladi — "
        "har oyning 1–7-kunlari soat {at} da, har kecha bitta sayt (qaysi saytligini "
        "sana belgilaydi); shu kunlarda oddiy make collect SOURCE=directories ham "
        "oʻsha kunning saytini toʻliq oʻqiydi. Saytni qoʻlda tanlash keyinroq "
        "quriladi. Hozir yangi kompaniyalar uchun: make collect SOURCE=directories."
    ),
    "variant_refused": (
        "{key} qoʻlda ishga tushirilmaydi — u faqat oʻz jadvali boʻyicha yuradi. "
        "Hozir mumkin: {allowed}."
    ),
    "no_row": "{key} uchun sources qatori yoʻq — avval make migrate.",
    "collectors_disabled": (
        ".env da COLLECTORS_ENABLED=false — hamma yigʻish oʻchiq. Uni true qilib "
        "make reload, keyin qaytadan make collect."
    ),
    "global_pause": (
        "Agent /toxtat bilan toʻxtatilgan — avval botda /yoq bilan davom ettiring, "
        "keyin qaytadan make collect."
    ),
    "disabled": (
        "{title} oʻchiq — avval botda /manbalar → {title} ni yoqing, keyin qaytadan "
        "make collect SOURCE={key}."
    ),
    "and_sites": (
        " Saytlaridan ham kamida bittasini yoqing (ular /manbalar roʻyxatining "
        "keyingi sahifasida — ▶️ bilan oʻting): {sites}."
    ),
    "no_site": (
        "{title} yoqilgan, lekin saytlaridan hech biri ishlamaydi: {sites}. Avval "
        "botda /manbalar → kamida bittasini yoqing (saytlar roʻyxatning keyingi "
        "sahifasida — ▶️ bilan oʻting; yolgʻiz «{title}» hech narsa oʻqimaydi), "
        "keyin qaytadan make collect SOURCE={key}."
    ),
    "no_pages": (
        "{title} uchun bugungi sahifa limiti tugagan — bugun boshqa sahifa "
        "oʻqilmaydi. Ertaga (Toshkent vaqti bilan 00:00 dan keyin) qayta urinib "
        "koʻring."
    ),
    "site_off": "{title} oʻchiq",
    "site_paused": "{title} {until} gacha pauzada",
    "paused": (
        "{title} {until} gacha toʻxtatilgan (Toshkent vaqti): {why}. Shundan keyin "
        "qaytadan urinib koʻring."
    ),
    "why_blocked": "sayt ({host}) 403/429 bilan javob berdi, agent {hours} soat kutadi",
    "why_low_precision": (
        "aniqligi past — botda /manbalar → {title} (⏸) ni bir marta bossangiz "
        "pauza tugaydi"
    ),
    "why_other": "sabab: {reason}",
    "already_running": (
        "{title} hozir yigʻilmoqda ({since} da boshlangan) — tugashini kuting va "
        "keyinroq qayta urinib koʻring; eng kechi {free} da boʻshaydi."
    ),
    "already_running_bare": (
        "{title} hozir yigʻilmoqda — tugashini kuting va keyinroq qayta urinib koʻring."
    ),
    "unavailable": "{title} hozir ishga tusha olmaydi ({note}).",
    # --- the run ---------------------------------------------------------------------
    "start": (
        "▶️ {title} ({key}) yigʻilmoqda… Saytlar sekin oʻqiladi (har biriga hurmat "
        "bilan): bir necha daqiqadan bir-ikki soatgacha ketishi mumkin. Toʻxtatish: "
        "Ctrl-C."
    ),
    "full_today": (
        "📅 Bugun oyning {day}-kuni — {sites} ning oylik toʻliq yurishi kuni: bu "
        "yurish uni toʻliq oʻqiydi (koʻproq sahifa va vaqt ketadi), qolgan "
        "saytlardan faqat yangilarini."
    ),
    "full_tonight": (
        " Kechki {at} dagi toʻliq yurish shu kunning qolgan sahifa limiti bilan "
        "ishlaydi."
    ),
    "stopped": (
        "⏹ Toʻxtatildi: yigʻish bekor qilindi, bu yurishda topilganlar yozilmadi. "
        "Keyingi yurish qaytadan oʻqiydi."
    ),
    "stopped_early": "⏹ Toʻxtatildi: yigʻish boshlanmadi, hech narsa yozilmadi.",
    "db_down": (
        "⛔ Baza javob bermayapti — yigʻish boshlanmadi. Avval make up (yoki make "
        "doctor), keyin qaytadan make collect SOURCE={key}."
    ),
    "db_down_during": (
        "⛔ Baza javob bermay qoldi — yigʻish tugamadi. Avval make up (yoki make "
        "doctor), keyin qaytadan make collect SOURCE={key}."
    ),
    "crashed": (
        "❌ Kutilmagan xato ({error}) — yigʻish tugamadi (tafsiloti yuqoridagi log "
        "qatorida). Keyinroq qayta urinib koʻring; takrorlansa botda /holat ni "
        "koʻring."
    ),
    "crashed_early": (
        "❌ Kutilmagan xato ({error}) — yigʻish boshlanmadi, hech narsa yozilmadi "
        "(tafsiloti yuqoridagi log qatorida). Keyinroq qayta urinib koʻring; "
        "takrorlansa botda /holat ni koʻring."
    ),
    # --- the summary -------------------------------------------------------------------
    "head_ok": "✅ {title} ({key}) — yigʻish tugadi, {took} davom etdi (yurish #{run}).",
    "head_partial": (
        "✅ {title} ({key}) — yigʻish tugadi, lekin hammasi oʻqilmadi; {took} davom "
        "etdi (yurish #{run})."
    ),
    "head_failed": (
        "❌ {title} ({key}) — yigʻish xato bilan tugadi: {error}; {took} davom etdi "
        "(yurish #{run})."
    ),
    "totals": "Oʻqildi: {pages} sahifa ({requests} soʻrov). Yangi yozuvlar: {new} ta.",
    "site": (
        "• {title} ({mode}): {pages} roʻyxat sahifasi, {cards} kompaniya kartasi, "
        "{rows} roʻyxat qatori; avval koʻrilgan {known} ta, yangi yozuv {new} ta."
    ),
    "site_skipped": "• {title}: oʻtkazildi — {why}.",
    "site_trouble": "  ⚠️ {what}.",
    "sites_not_built": "• Hali qurilmagan saytlar (oʻtkazildi): {sites}.",
    "sites_not_reached": "• Yurish bu saytlargacha yetmadi: {sites}.",
    "mode_diff": "yangilari",
    "mode_full": "toʻliq",
    "skip_disabled": "/manbalar da oʻchiq",
    "skip_paused": "pauzada",
    "skip_paused_until": "{until} gacha pauzada",
    "skip_no_sources_row": "sources qatori yoʻq",
    "t_robots": "robots.txt taqiqladi: {n}",
    "t_card_errors": "karta olinmadi: {n}",
    "t_parse_errors": "sahifani oʻqib boʻlmadi: {n}",
    "t_dropped": "2024 dan eski karta tashlandi: {n}",
    "t_down": "sayt javob bermay qoldi",
    "t_status": "roʻyxat javoblari: {codes}",
    "list": (
        "• {show} {year}: {status} — {kept} ta oʻzbek kompaniya ({rows} qatordan), "
        "{pages} sahifa{archive}."
    ),
    "list_archive": ", arxiv",
    "list_error": " ({error})",
    "no_lists": (
        "• Bu hafta oʻqiladigan koʻrgazma roʻyxati yoʻq: oynasi ochiq koʻrgazma yoʻq, "
        "arxivlar avval oʻqilgan."
    ),
    "list_complete": "toʻliq oʻqildi",
    "list_truncated": "chala oʻqildi",
    "list_removed": "saytdan olib tashlangan",
    "list_missing": "roʻyxat hali chiqmagan",
    "list_failed": "oʻqib boʻlmadi",
    "list_robots": "robots.txt taqiqlaydi",
    "calendar": (
        "⚠️ Koʻrgazmalar kalendarida {n} ta notoʻgʻri yozuv bor "
        "(sources.exhibitors.config.calendar)."
    ),
    "w_robots": "⚠️ robots.txt taqiqlagani uchun {n} ta sahifa oʻqilmadi.",
    "w_pages": "⚠️ Bugungi sahifa limiti tugadi — qolgani keyingi yurishda oʻqiladi.",
    "w_usd": "⚠️ Manbaning bugungi pul limiti tugadi — qolgani keyingi yurishda.",
    "w_deadline": "⚠️ Yurish vaqti tugadi — qolgani keyingi yurishda oʻqiladi.",
    "w_cost": "⚠️ Xarajat limiti ({code}) — pullik soʻrov yuborilmadi.",
    "w_pause": "⚠️ /toxtat bosildi — yigʻish yarim yoʻlda toʻxtatildi.",
    "w_blocked": (
        "⛔ Sayt toʻsdi (403/429): {hosts} — manba {hours} soatga pauzaga qoʻyildi."
    ),
    "w_captcha": "⚠️ CAPTCHA sahifalari: {n} ta (oʻqilmadi).",
    "w_incomplete": (
        "⚠️ {n} ta roʻyxat yoki sayt toʻliq oʻqilmadi (tafsiloti yurish #{run} da)."
    ),
    "w_dropped": "⚠️ Baza {n} ta yozuvni qabul qilmadi.",
    "err_blocked": "sayt toʻsdi (403/429)",
    "err_cancelled": "toʻxtatildi",
    "err_abandoned": "yarim yoʻlda qolgan",
    "next": (
        "➡️ Topilgan kompaniyalarni ishlab turgan worker oʻz jadvali boʻyicha "
        "tekshiradi va baholaydi; kartalar botga dushanba–shanba {start}–{end} "
        "oraligʻida keladi, kuniga koʻpi bilan {cap} ta."
    ),
    "no_new": "Bu yurishda yangi yozuv topilmadi — botga undan yangi karta kelmaydi.",
    "retry": "↻ Keyinroq qayta urinib koʻring; takrorlansa botda /holat ni koʻring.",
    "took_s": "{s} soniya",
    "took_m": "{m} daqiqa {s} soniya",
    "took_h": "{h} soat {m} daqiqa",
}

# The collectors another process or another command runs: named, not offered.
ELSEWHERE: Final[Mapping[str, str]] = {
    TelegramMonitor.key: TEXT["telegram"],
    ErpSync.key: TEXT["erp"],
}
# Built variant slots a manual run refuses with their own sentence (a TEXT
# key, the module docstring: FULL=1).
NO_MANUAL: Final[Mapping[str, str]] = {"directories_full": "full_refused"}
# Built variant (non-primary) slots a manual run may start. None today: any
# other variant a later task builds is refused («faqat oʻz jadvali boʻyicha»)
# until somebody judges a daytime run of its cadence and names it here
# (tests/test_collect.py's fence turns red until then).
MANUAL_VARIANTS: Final[frozenset[str]] = frozenset()
# The runner's skip notes (runner.skip_reason, its claim) said in words.
SKIP_TEXT: Final[Mapping[str, str]] = {
    "collectors_disabled": TEXT["collectors_disabled"],
    "global_pause": TEXT["global_pause"],
}
LIST_STATUS: Final[Mapping[str, str]] = {
    status: TEXT[f"list_{status}"]
    for status in ("complete", "truncated", "removed", "missing", "failed", "robots")
}
EXHAUSTED: Final[Mapping[str, str]] = {
    "pages": TEXT["w_pages"],
    "usd": TEXT["w_usd"],
    "deadline": TEXT["w_deadline"],
    runner.GLOBAL_PAUSE: TEXT["w_pause"],
}
ERRORS: Final[Mapping[str, str]] = {
    BLOCKED_ERROR: TEXT["err_blocked"],
    runner.CANCELLED: TEXT["err_cancelled"],
    runner.ABANDONED: TEXT["err_abandoned"],
}
# The per-site counters said under a site's line when not zero.
SITE_TROUBLE: Final = ("robots", "card_errors", "parse_errors", "dropped")


@dataclass(frozen=True, slots=True)
class Refused:
    """Nothing ran: one sentence for the owner."""

    text: str


@dataclass(frozen=True, slots=True)
class Outcome:
    lines: tuple[str, ...]
    code: int


# --- which slot (pure: the worker's table) ----------------------------------------


def is_collector(slot: JobSlot) -> bool:
    """A built collector slot: its job is set and it names a sources row."""
    return slot.job is not None and slot.source_key is not None


def offered(table: Iterable[JobSlot] | None = None) -> tuple[JobSlot, ...]:
    """The primary built collector slots a manual run may start."""
    return tuple(
        slot
        for slot in (worker.job_table() if table is None else table)
        if is_collector(slot) and slot.primary and slot.source_key not in ELSEWHERE
    )


def allowed(table: Iterable[JobSlot] | None = None) -> tuple[str, ...]:
    """The SOURCE values ``make collect`` takes today, in the table's order."""
    return tuple(slot.id for slot in offered(table))


def choose(
    key: str, variant: str | None = None, table: Iterable[JobSlot] | None = None
) -> JobSlot | Refused:
    """The slot ``make collect SOURCE=<key> [FULL=1]`` runs, or why none.
    A variant slot's own id (``directories_full``) is its source's with that
    variant."""
    slots = tuple(worker.job_table() if table is None else table)
    names = ", ".join(allowed(slots))
    key = key.strip()
    if not key:
        return Refused(TEXT["usage"].format(allowed=names))
    slot = next((s for s in slots if s.id == key), None)
    if slot is None:
        return Refused(TEXT["unknown"].format(key=key, allowed=names))
    elsewhere = ELSEWHERE.get(slot.source_key or "")
    if elsewhere is not None:
        return Refused(elsewhere)
    if slot.job is None:
        return Refused(TEXT["not_built"].format(key=key, allowed=names))
    if slot.source_key is None:
        # A job that collects nothing (backup, resolve …).
        return Refused(TEXT["unknown"].format(key=key, allowed=names))
    wanted = variant or slot.variant
    chosen = next(
        (
            s
            for s in slots
            if is_collector(s) and s.source_key == slot.source_key and s.variant == wanted
        ),
        None,
    )
    if chosen is None:
        return Refused(TEXT["no_variant"].format(key=key, variant=wanted))
    if chosen.id in NO_MANUAL:
        return Refused(TEXT[NO_MANUAL[chosen.id]].format(at=clock_of(chosen)))
    if not chosen.primary and chosen.id not in MANUAL_VARIANTS:
        return Refused(TEXT["variant_refused"].format(key=chosen.id, allowed=names))
    return chosen


def clock_of(slot: JobSlot) -> str:
    """The time of day a slot's first cron fires (``0 23 1-7 * *`` → 23:00)."""
    minute, hour = [*slot.triggers[0].split(), "", ""][:2]
    if minute.isdigit() and hour.isdigit():
        return f"{int(hour):02d}:{int(minute):02d}"
    return slot.triggers[0]


def full_slot(table: Iterable[JobSlot] | None = None) -> JobSlot | None:
    """The directories' monthly full crawl (the ``full`` variant slot)."""
    return next(
        (
            s
            for s in (worker.job_table() if table is None else table)
            if s.source_key == directories.PARENT and s.variant == FULL and s.job
        ),
        None,
    )


# --- may it run now (the runner's and the fan-out's own rules) ---------------------


def at(moment: datetime) -> str:
    """Tashkent time as the bot prints it (``bot/pages.at``)."""
    return f"{moment.astimezone(settings.tz):%d.%m %H:%M}"


def pause_why(source: Source) -> str:
    reason = source.paused_reason or ""
    if reason == erp_feeds.LOW_PRECISION_REASON:
        return TEXT["why_low_precision"].format(title=source.title_uz)
    if reason.startswith(f"{BLOCKED_ERROR}:"):
        return TEXT["why_blocked"].format(
            host=reason.split(":", 1)[1],
            hours=int(BREAKER_PAUSE.total_seconds() // 3600),
        )
    return TEXT["why_other"].format(reason=reason or "—")


@dataclass(frozen=True, slots=True)
class Sites:
    """The directories' sites as the run would see them now: those it would
    crawl, those of them it would crawl in full (the 1st–7th's site), and
    every built site's own switch with its state."""

    running: tuple[str, ...]
    full: tuple[str, ...]
    switches: tuple[str, ...]


async def directory_sites(variant: str | None, now: datetime) -> Sites:
    """``Directories.collect``'s own reading: ``plan`` on the parent's cron,
    then ``directories.skip_reason`` per site."""
    rows = await directories.sub_rows()
    due = directories.plan(variant, now, diff_due=directories.diff_due(rows.cron, now))
    going = [
        (sub, mode)
        for sub, mode in due
        if directories.skip_reason(sub, rows.states.get(sub.source_key), now) is None
    ]
    own = [
        sub
        for sub in directories.SUBSOURCES
        if sub.spec is not None and sub.source_key != directories.PARENT
    ]
    titles = await source_titles(sub.source_key for sub in own)
    switches: list[str] = []
    for sub in own:
        title = titles.get(sub.source_key, sub.host)
        state = rows.states.get(sub.source_key)
        if state is None or not state.enabled:
            switches.append(TEXT["site_off"].format(title=title))
        elif state.paused_until is not None and state.paused_until > now:
            until = at(state.paused_until)
            switches.append(TEXT["site_paused"].format(title=title, until=until))
    return Sites(
        running=tuple(sub.site for sub, _mode in going),
        full=tuple(
            titles.get(sub.source_key, sub.host) for sub, mode in going if mode == "full"
        ),
        switches=tuple(switches),
    )


def full_today(sites: Sites, now: datetime) -> str | None:
    """Said before a directories run that crawls a site in full (a plain run
    on the 1st–7th, ``plan(None, …)``), so the owner knows the run is the
    big one and that the 23:00 full crawl gets what is left of the day."""
    if not sites.full:
        return None
    local = now.astimezone(settings.tz)
    text = TEXT["full_today"].format(day=local.day, sites=", ".join(sites.full))
    night = full_slot()
    if night is not None:
        clock = clock_of(night)
        with contextlib.suppress(ValueError):
            if local.time() < time.fromisoformat(clock):
                text += TEXT["full_tonight"].format(at=clock)
    return text


async def source_titles(keys: Iterable[str]) -> dict[str, str]:
    wanted = list(dict.fromkeys(keys))
    async with SessionLocal() as session:
        rows = await session.execute(
            sa.select(Source.key, Source.title_uz).where(Source.key.in_(wanted))
        )
        return {str(key): str(title) for key, title in rows.all()}


@dataclass(frozen=True, slots=True)
class Going:
    """The source's ``running`` row: when it started, and the latest it
    frees the source (its deadline plus the runner's stale margin: a process
    that died with it open is overtaken then)."""

    since: datetime
    free: datetime


async def running_row(source_key: str) -> Going | None:
    async with SessionLocal() as session:
        found = (
            await session.execute(
                sa.select(CollectorRun.started_at, CollectorRun.meta)
                .where(
                    CollectorRun.source_key == source_key,
                    CollectorRun.status == "running",
                )
                .order_by(CollectorRun.started_at)
                .limit(1)
            )
        ).first()
    if found is None:
        return None
    started, meta = found
    due = started + runner.DEFAULT_DEADLINE
    stamp = (meta or {}).get("deadline_at")
    if isinstance(stamp, str):
        with contextlib.suppress(ValueError):
            due = datetime.fromisoformat(stamp)
    return Going(since=started, free=due + runner.STALE_MARGIN)


async def said(
    note: str, source: Source, *, now: datetime, sites: Sites | None = None
) -> str:
    """A runner skip note (``runner.skip_reason``, its ``already_running``
    claim, a collector's ``unavailable``) as the owner's sentence."""
    title, key = source.title_uz, source.key
    if note in SKIP_TEXT:
        return SKIP_TEXT[note]
    if note == "source_disabled":
        text = TEXT["disabled"].format(title=title, key=key)
        if sites is not None and not sites.running and sites.switches:
            text += TEXT["and_sites"].format(sites=", ".join(sites.switches))
        return text
    if note == "paused" and source.paused_until is not None:
        return TEXT["paused"].format(
            title=title, until=at(source.paused_until), why=pause_why(source)
        )
    if note == "already_running":
        going = await running_row(key)
        if going is None:
            return TEXT["already_running_bare"].format(title=title)
        return TEXT["already_running"].format(
            title=title, since=at(going.since), free=at(going.free)
        )
    return TEXT["unavailable"].format(title=title, note=note)


@dataclass(frozen=True, slots=True)
class Ready:
    """It may run: the lines said after the start line, before the crawl."""

    notes: tuple[str, ...] = ()


async def check(slot: JobSlot, *, now: datetime) -> Refused | Ready:
    """Why ``slot`` may not run now, before anything is claimed or fetched:
    the runner's own ``skip_reason`` on its row, for the directories the
    fan-out's per-site rule, then the day's page budget (the runner's own
    ``build_budget``, read only). Ready: it may, with what to say first."""
    assert slot.source_key is not None
    async with SessionLocal() as session:
        source = await session.get(Source, slot.source_key)
        if source is None:
            return Refused(TEXT["no_row"].format(key=slot.source_key))
        note = await runner.skip_reason(session, source, now)
        budget = (
            None
            if note is not None
            else await runner.build_budget(
                session, source, now=now, deadline=runner.DEFAULT_DEADLINE
            )
        )
    sites = (
        await directory_sites(slot.variant, now)
        if slot.source_key == directories.PARENT
        else None
    )
    if note is not None:
        return Refused(await said(note, source, now=now, sites=sites))
    if sites is not None and not sites.running:
        return Refused(
            TEXT["no_site"].format(
                title=source.title_uz, key=slot.id, sites=", ".join(sites.switches)
            )
        )
    if budget is not None and budget.pages <= 0:
        return Refused(TEXT["no_pages"].format(title=source.title_uz))
    notes = full_today(sites, now) if sites is not None else None
    return Ready(notes=(notes,) if notes else ())


async def refusal(slot: JobSlot, *, now: datetime) -> Refused | None:
    """:func:`check`'s refusal, or None: it may run."""
    checked = await check(slot, now=now)
    return checked if isinstance(checked, Refused) else None


# --- the summary: the run's own row ----------------------------------------------


def took(start: datetime, end: datetime | None) -> str:
    seconds = max(0, int(((end or start) - start).total_seconds()))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    if h:
        return TEXT["took_h"].format(h=h, m=m)
    if m:
        return TEXT["took_m"].format(m=m, s=s)
    return TEXT["took_s"].format(s=s)


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _new_of(meta: Mapping[str, Any], source_key: str) -> int:
    written = meta.get("sources")
    found = written.get(source_key) if isinstance(written, Mapping) else None
    return _count(found.get("observations")) if isinstance(found, Mapping) else 0


def site_lines(
    meta: Mapping[str, Any],
    titles: Mapping[str, str],
    paused: Mapping[str, datetime | None] | None = None,
) -> list[str]:
    """The directories, one line per site the run named (``meta.subsources``,
    in ``SUBSOURCES`` order), its new rows from ``meta.sources``; a paused
    site with its row's ``paused_until`` (``paused``); and the built sites a
    plain run planned (``plan(None, …)``: every site) but never reached, its
    budget or deadline spent before them (unbuilt ones are named as such)."""
    subs = meta.get("subsources")
    if not isinstance(subs, Mapping):
        return []
    lines: list[str] = []
    not_built: list[str] = []
    not_reached: list[str] = []
    for sub in directories.SUBSOURCES:
        title = (
            titles.get(sub.source_key, sub.host)
            if sub.source_key != directories.PARENT
            else sub.host
        )
        data = subs.get(sub.site)
        if not isinstance(data, Mapping):
            if sub.spec is None:
                not_built.append(title)
            elif meta.get("variant") is None:
                not_reached.append(title)
            continue
        skipped = data.get("skipped")
        if skipped == "not_built":
            not_built.append(title)
            continue
        if isinstance(skipped, str):
            why = TEXT.get(f"skip_{skipped}", skipped)
            until = (paused or {}).get(sub.source_key)
            if skipped == "paused" and until is not None:
                why = TEXT["skip_paused_until"].format(until=at(until))
            lines.append(TEXT["site_skipped"].format(title=title, why=why))
            continue
        lines.append(
            TEXT["site"].format(
                title=title,
                mode=TEXT.get(f"mode_{data.get('mode')}", str(data.get("mode"))),
                pages=_count(data.get("pages")),
                cards=_count(data.get("cards")),
                rows=_count(data.get("rows")),
                known=_count(data.get("known")),
                new=_new_of(meta, sub.source_key),
            )
        )
        trouble = [
            TEXT[f"t_{name}"].format(n=_count(data.get(name)))
            for name in SITE_TROUBLE
            if _count(data.get(name))
        ]
        stops = data.get("stops")
        if isinstance(stops, Mapping) and (stops.get("transport") or stops.get("http")):
            trouble.append(TEXT["t_down"])
        answers = data.get("list_status")
        if isinstance(answers, Mapping) and answers:
            codes = ", ".join(f"{c}×{_count(n)}" for c, n in sorted(answers.items()))
            trouble.append(TEXT["t_status"].format(codes=codes))
        if trouble:
            lines.append(TEXT["site_trouble"].format(what=" · ".join(trouble)))
    if not_reached:
        lines.append(TEXT["sites_not_reached"].format(sites=", ".join(not_reached)))
    if not_built:
        lines.append(TEXT["sites_not_built"].format(sites=", ".join(not_built)))
    return lines


def list_lines(meta: Mapping[str, Any]) -> list[str]:
    """The exhibitors, one line per list the run read (``meta.lists``)."""
    lists = meta.get("lists")
    if not isinstance(lists, list):
        return []
    if not lists:
        return [TEXT["no_lists"]]
    lines: list[str] = []
    for item in lists:
        if not isinstance(item, Mapping):
            continue
        error = item.get("error")
        status = LIST_STATUS.get(str(item.get("status")), str(item.get("status")))
        if error:
            status += TEXT["list_error"].format(error=error)
        lines.append(
            TEXT["list"].format(
                show=item.get("show"),
                year=item.get("year") or "",
                status=status,
                kept=_count(item.get("kept")),
                rows=_count(item.get("rows")),
                pages=_count(item.get("pages")),
                archive=TEXT["list_archive"] if item.get("why") == "archive" else "",
            )
        )
    calendar = meta.get("calendar")
    if isinstance(calendar, list) and calendar:
        lines.append(TEXT["calendar"].format(n=len(calendar)))
    return lines


def warning_lines(run: CollectorRun) -> list[str]:
    """What stopped or cut the run, from its ``meta`` (any collector)."""
    meta: Mapping[str, Any] = run.meta or {}
    lines: list[str] = []
    robots = _count(meta.get("skipped_robots"))
    if robots:
        lines.append(TEXT["w_robots"].format(n=robots))
    exhausted = meta.get("exhausted")
    if exhausted in ("cost_cap", "refused"):
        lines.append(TEXT["w_cost"].format(code=meta.get("refused") or exhausted))
    elif isinstance(exhausted, str) and exhausted in EXHAUSTED:
        lines.append(EXHAUSTED[exhausted])
    blocked = meta.get("blocked_hosts")
    if isinstance(blocked, list) and blocked:
        lines.append(
            TEXT["w_blocked"].format(
                hosts=", ".join(str(h) for h in blocked),
                hours=int(BREAKER_PAUSE.total_seconds() // 3600),
            )
        )
    captcha = _count(meta.get("captcha"))
    if captcha:
        lines.append(TEXT["w_captcha"].format(n=captcha))
    incomplete = meta.get("incomplete")
    if isinstance(incomplete, list) and incomplete:
        lines.append(TEXT["w_incomplete"].format(n=len(incomplete), run=run.id))
    dropped = meta.get("dropped")
    if isinstance(dropped, Mapping):
        refused = sum(_count(n) for n in dropped.values())
        if refused:
            lines.append(TEXT["w_dropped"].format(n=refused))
    return lines


def next_line() -> str:
    """Where the companies go from here (06 §1.6: REVIEW_HOURS, Mon–Sat,
    REVIEW_DAILY_CAP owner cards a day — ``reviews.sending_hours``)."""
    start, end = settings.review_hours_parsed
    return TEXT["next"].format(
        start=f"{start:%H:%M}", end=f"{end:%H:%M}", cap=settings.review_daily_cap
    )


def render(
    run: CollectorRun,
    *,
    title: str,
    titles: Mapping[str, str],
    paused: Mapping[str, datetime | None] | None = None,
) -> list[str]:
    """The summary of one finished run, read off its ``collector_runs`` row:
    the head, the totals, per site (directories) or per list (exhibitors),
    what cut it, where its companies go next (or that it found none), and
    for a failed run what to do."""
    meta: Mapping[str, Any] = run.meta or {}
    when = took(run.started_at, run.finished_at)
    head = {"ok": "head_ok", "partial": "head_partial"}.get(run.status, "head_failed")
    error = ERRORS.get(run.error or "", run.error or run.status)
    lines = [
        TEXT[head].format(
            title=title, key=run.source_key, took=when, run=run.id, error=error
        ),
        TEXT["totals"].format(
            pages=run.pages, requests=run.requests, new=run.new_observations
        ),
    ]
    lines += site_lines(meta, titles, paused)
    lines += list_lines(meta)
    lines += warning_lines(run)
    if run.new_observations:
        lines.append(next_line())
    elif run.status != "failed":
        lines.append(TEXT["no_new"])
    if exit_code(run.status) != EXIT_DONE:
        lines.append(TEXT["retry"])
    return lines


def exit_code(status: str) -> int:
    return EXIT_DONE if status in ("ok", "partial") else EXIT_FAILED


async def summary(run_id: int) -> Outcome:
    """The run's own row, rendered (never a count of this tool's own)."""
    async with SessionLocal() as session:
        run = await session.get(CollectorRun, run_id)
        if run is None:
            raise LookupError(f"collector run {run_id} is gone")
        rows = (
            await session.execute(
                sa.select(Source.key, Source.title_uz, Source.paused_until)
            )
        ).all()
    titles = {str(key): str(value) for key, value, _ in rows}
    paused = {str(key): until for key, _, until in rows}
    title = titles.get(run.source_key, run.source_key)
    lines = render(run, title=title, titles=titles, paused=paused)
    return Outcome(tuple(lines), exit_code(run.status))


# --- the run ----------------------------------------------------------------------


Say = Callable[[str], None]


@dataclass(slots=True)
class Progress:
    """How far a run got: ``started`` once the slot's job was handed over
    (what a stop or a failure is then said as: «tugamadi», not
    «boshlanmadi»)."""

    started: bool = False


async def collect(
    slot: JobSlot,
    *,
    now: datetime,
    say: Say | None = None,
    progress: Progress | None = None,
) -> Outcome:
    """Refuse, or run ``slot``'s own job once (``worker.collect_now``) and
    read its run back. ``say`` hears the start line (and, on the 1st–7th, the
    directories' full-crawl note) before the crawl."""
    checked = await check(slot, now=now)
    if isinstance(checked, Refused):
        return Outcome((checked.text,), EXIT_REFUSED)
    assert slot.source_key is not None
    titles = await source_titles([slot.source_key])
    if say is not None:
        say(TEXT["start"].format(title=titles.get(slot.source_key, slot.id), key=slot.id))
        for note in checked.notes:
            say(note)
    log.info("manual collect", extra={"job": slot.id, "variant": slot.variant})
    if progress is not None:
        progress.started = True
    report = await worker.collect_now(slot)
    if report.status == "skipped":
        # The runner's own refusal, met after ours (another run claimed it),
        # or another make collect holding the source's lock.
        async with SessionLocal() as session:
            source = await session.get(Source, slot.source_key)
        note = str(report.meta.get("note") or "")
        if source is None:
            return Outcome((TEXT["no_row"].format(key=slot.source_key),), EXIT_REFUSED)
        return Outcome((await said(note, source, now=now),), EXIT_REFUSED)
    if report.run_id is None:
        raise LookupError(f"{slot.id}: the runner reported no run")
    return await summary(report.run_id)


async def until_stopped(work: asyncio.Future[Outcome]) -> Outcome | None:
    """``work`` until it ends, or until SIGINT/SIGTERM (Ctrl-C, ``docker
    stop``): then it is cancelled — the runner closes its own row
    ``failed``/``cancelled``, so the next run is not refused
    ``already_running`` — and None is returned."""
    stop = asyncio.Event()
    installed = install_stop_signals(stop)
    waiter = asyncio.ensure_future(stop.wait())
    try:
        pending: set[asyncio.Future[Any]] = {work, waiter}
        await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        if work.done():
            return work.result()
        work.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await work
        return None
    finally:
        waiter.cancel()
        remove_stop_signals(installed)


# The database not answering (``make down``, a restart, the wrong port): a
# foreseeable state with a next step, not «unexpected».
DB_DOWN: Final = (
    sa.exc.OperationalError,
    sa.exc.InterfaceError,
    psycopg.OperationalError,
    ConnectionError,
)


async def _main(slot: JobSlot, progress: Progress | None = None) -> int:
    progress = progress if progress is not None else Progress()
    try:
        work = asyncio.ensure_future(
            collect(slot, now=datetime.now(settings.tz), say=_say, progress=progress)
        )
        outcome = await until_stopped(work)
    except DB_DOWN as exc:
        log.error(
            "make collect: the database did not answer",
            extra={"job": slot.id, "error": type(exc).__name__},
        )
        said_ = "db_down_during" if progress.started else "db_down"
        print(TEXT[said_].format(key=slot.id))
        return EXIT_FAILED
    except Exception as exc:
        # The run's own failures end in its row; this is the tool's (a
        # bookkeeping write refused, a bug). Logged redacted, the owner's
        # line last.
        log.error("make collect failed", exc_info=True, extra={"job": slot.id})
        said_ = "crashed" if progress.started else "crashed_early"
        print(TEXT[said_].format(error=type(exc).__name__))
        return EXIT_FAILED
    finally:
        with contextlib.suppress(Exception):
            await engine.dispose()
    if outcome is None:
        print(TEXT["stopped" if progress.started else "stopped_early"])
        return EXIT_FAILED
    print("\n".join(outcome.lines))
    return outcome.code


def _say(line: str) -> None:
    print(line, flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    """``worker.main collect <slot> [<variant>]``: a refusal the table alone
    decides is said before any connection is opened. The arguments are read
    as plain words (no option parser: ``-h`` is a source name nobody has, not
    an English help page); a Ctrl-C the run's own handler did not catch (the
    first moments, the last) is the stop sentence, not a traceback."""
    args = list(sys.argv[1:] if argv is None else argv)
    progress = Progress()
    try:
        if len(args) > 2:
            picked: JobSlot | Refused = Refused(
                TEXT["usage"].format(allowed=", ".join(allowed()))
            )
        else:
            source = args[0] if args else ""
            variant = args[1] if len(args) > 1 else ""
            picked = choose(source, variant.strip() or None)
        if isinstance(picked, Refused):
            print(picked.text)
            return EXIT_REFUSED
        return asyncio.run(_main(picked, progress))
    except KeyboardInterrupt:
        print(TEXT["stopped" if progress.started else "stopped_early"])
        return EXIT_FAILED


__all__ = [
    "ELSEWHERE",
    "MANUAL_VARIANTS",
    "NO_MANUAL",
    "TEXT",
    "Outcome",
    "Progress",
    "Ready",
    "Refused",
    "allowed",
    "check",
    "choose",
    "collect",
    "main",
    "offered",
    "refusal",
    "render",
    "summary",
]
