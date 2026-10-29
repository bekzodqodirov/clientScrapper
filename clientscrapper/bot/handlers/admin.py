# VENDORED_FROM dailyJournal@6b1f67f miya/bot/handlers.py
"""The owner's writes (06 §1.2): ``/toxtat``, ``/yoq`` (``ps:*``),
``/import`` (``im:*``), ``/unut``, ``/dnc`` (``dn:*``), ``/vazn``,
``/push tekshir`` (``pt:*``) and ``/stir``.

Each write is the service's that owns it — ``owner`` (pause, the CRM
breaker, /stir), ``client_import``, ``suppression.dnc_command``,
``scoring.owner_edit``, ``crm_push.push_tekshir_*`` — in the handler's one
transaction. A destructive write waits for its confirm button; ``/toxtat``
does not (a safety action is at once). ``/unut`` is the purge's
(``services/purge``, 06 §5.4): the key is looked up as ``/qidir`` looks it
up, one live company is planned (its ``purge_log`` row written, the plan kept
in memory under that id), the card shows counts and the uuid — never a name
— and only ``pg:<purge_log_id>:y`` executes; a cancel or a confirm that
outlived the plan closes the row, a second tap on a plan the first one took
closes nothing, and a purge that stopped part-way tells the owner to send
``/unut <uuid>`` again (the company is planned again while it holds anything).
"""

from __future__ import annotations

import io
import logging
import uuid
from dataclasses import dataclass
from typing import Final

from aiogram.types import Document, Message
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.bot import keyboards, pages
from clientscrapper.bot.cards_out import BotCards
from clientscrapper.bot.handlers.common import (
    Deps,
    Tap,
    args_of,
    edit,
    reply,
    toast,
)
from clientscrapper.bot.texts import t
from clientscrapper.db.models import Company
from clientscrapper.services import (
    cards,
    client_import,
    companies,
    crm_push,
    owner,
    purge,
    scoring,
    suppression,
)
from clientscrapper.services.cards import esc

log = logging.getLogger(__name__)

# The Bot API hands a bot files up to 20 MB; the ERP's client export is far
# below it.
IMPORT_MAX_BYTES: Final = 20 * 1000 * 1000
PLAN_IMPORT: Final = "import"
PLAN_DNC: Final = "dnc"
PLAN_PURGE: Final = "purge"
# /unut's reason (06 §5.4): a company the owner tells the agent to forget is
# never carded or pushed again — suppression(legal) on every key it had.
PURGE_REASON: Final = "legal_request"
PURGE_CANCELLED: Final = "cancelled"
PURGE_EXPIRED: Final = "expired"


def _owner(message: Message) -> str:
    user = message.from_user.id if message.from_user else 0
    return owner.owner_ref(user)


# --- /toxtat, /yoq ----------------------------------------------------------------------


async def toxtat(message: Message, deps: Deps) -> None:
    async with deps.session_factory() as session:
        await owner.pause(
            session, by=_owner(message), reason=args_of(message) or None, now=deps.clock()
        )
    await reply(message, t("toxtat.done"))


async def yoq(message: Message, deps: Deps) -> None:
    crm = args_of(message).lower() in {"crm", "erp"}
    now = deps.clock()
    async with deps.session_factory() as session:
        if crm:
            until = await owner.erp_breaker_until(session)
        else:
            held = await owner.paused(session)
    if crm:
        if until is None or until <= now:
            await reply(message, t("yoq.no_breaker"))
            return
        text = t("yoq.ask_crm", until=pages.at(until))
        await reply(message, text, keyboard=keyboards.resume_keyboard(crm=True))
        return
    if not held:
        await reply(message, t("yoq.not_paused"))
        return
    await reply(message, t("yoq.ask"), keyboard=keyboards.resume_keyboard())


async def on_resume(tap: Tap) -> None:
    async with tap.deps.session_factory() as session:
        was = await owner.resume(session)
    word = "yoq.resumed" if was else "yoq.not_paused"
    await edit(tap.query, t(word))
    await toast(tap.query)


async def on_keep(tap: Tap) -> None:
    async with tap.deps.session_factory() as session:
        held = await owner.paused(session)
    await edit(tap.query, t("yoq.kept" if held else "yoq.unchanged"))
    await toast(tap.query)


async def on_clear_crm(tap: Tap) -> None:
    async with tap.deps.session_factory() as session:
        cleared = await owner.clear_erp_breaker(session)
    await edit(tap.query, t("yoq.breaker_cleared" if cleared else "yoq.no_breaker"))
    await toast(tap.query)


# --- /import ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ImportPlanned:
    plan: client_import.ImportPlan
    accept_fewer: bool


def _document(message: Message) -> Document | None:
    if message.document is not None:
        return message.document
    replied = message.reply_to_message
    return replied.document if replied is not None else None


async def import_command(message: Message, deps: Deps) -> None:
    document = _document(message)
    if document is None:
        await reply(message, t("import.how"))
        return
    if (document.file_size or 0) > IMPORT_MAX_BYTES:
        mb = (document.file_size or 0) // 1_000_000
        await reply(message, t("import.too_big", mb=mb))
        return
    bot = message.bot
    if bot is None:
        await reply(message, t("import.download_failed"))
        return
    try:
        buffer = await bot.download(document.file_id, destination=io.BytesIO())
    except Exception as exc:
        log.warning("import file not downloaded", extra={"error": type(exc).__name__})
        buffer = None
    if buffer is None:
        await reply(message, t("import.download_failed"))
        return
    data = buffer.read()
    name = document.file_name or "clients.xlsx"
    try:
        export = client_import.read_rows(name, data=data)
        async with deps.session_factory() as session:
            plan = await client_import.plan_import(session, export)
    except client_import.BadFile as bad:
        await reply(message, esc(bad.text()))
        return
    fewer = plan.refusal == "fewer"
    plan_id = deps.plans.put(PLAN_IMPORT, ImportPlanned(plan, fewer), now=deps.clock())
    text = "\n".join(esc(line) for line in client_import.plan_lines(plan))
    text += "\n" + t("import.ask_fewer" if fewer else "import.ask")
    await reply(message, text, keyboard=keyboards.import_keyboard(plan_id))


async def on_import(tap: Tap) -> None:
    plan_id = tap.data.ref
    assert plan_id is not None
    # Answered first: a large file's write may outlast the ~15 s a button
    # waits for its answer; the outcome is the edited message.
    await toast(tap.query)
    now = tap.deps.clock()
    planned = tap.deps.plans.take(PLAN_IMPORT, plan_id, now=now)
    if not isinstance(planned, ImportPlanned):
        await edit(tap.query, t("import.expired"))
        return
    if tap.data.action != "y":
        await edit(tap.query, t("import.cancelled"))
        return
    async with tap.deps.session_factory() as session:
        applied = await client_import.apply_import(
            session, planned.plan, now=now, accept_fewer=planned.accept_fewer
        )
    await edit(tap.query, esc(client_import.applied_line(applied)))


# --- /unut ---------------------------------------------------------------------------


def _handle(arg: str) -> bool:
    """A Telegram ``@username`` or ``t.me/`` link, as ``cards.lookup`` reads one."""
    text = arg.strip()
    return text.startswith("@") or "t.me/" in text.lower()


async def unut(message: Message, deps: Deps) -> None:
    arg = args_of(message)
    if not arg:
        await reply(message, t("unut.how"))
        return
    held: purge.PurgePlan | None = None
    async with deps.session_factory() as session:
        found, by_name = await cards.purge_roots(session, arg)
        if len(found) == 1:
            plan = await purge.plan_company(session, found[0], reason=PURGE_REASON)
            purge_log_id = await purge.record(session, plan, requested_by=_owner(message))
        elif not found and _handle(arg):
            # A person kept under no contact by that name: their messages,
            # by the ids the username was seen on (purge.plan_handle).
            held = await purge.plan_handle(session, arg, reason=PURGE_REASON)
            if held is not None:
                purge_log_id = await purge.record(
                    session, held, requested_by=_owner(message)
                )
    if held is not None:
        deps.plans.put(PLAN_PURGE, held, now=deps.clock(), plan_id=purge_log_id)
        keyboard = None if held.empty else keyboards.purge_keyboard(purge_log_id)
        await reply(message, pages.purge_plan_text(held), keyboard=keyboard)
        return
    if not found:
        await reply(message, t("unut.none", query=esc(arg)))
        return
    if len(found) > 1:
        ids = "\n".join(f"<code>{company_id}</code>" for company_id in found)
        await reply(message, t("unut.many", n=len(found), ids=ids))
        return
    deps.plans.put(PLAN_PURGE, plan, now=deps.clock(), plan_id=purge_log_id)
    keyboard = None if plan.empty else keyboards.purge_keyboard(purge_log_id)
    await reply(message, pages.purge_plan_text(plan, by_name=by_name), keyboard=keyboard)


# What a tap on a plan that will not run (again) says, by its log row
# (``purge.row_state``): executed, being executed or stopped, failed part-way,
# or closed.
ROW_TEXT: Final = {
    purge.ROW_DONE: "unut.done",
    purge.ROW_RUNNING: "unut.running",
    purge.ROW_FAILED: "unut.unfinished",
    purge.ROW_CLOSED: "unut.closed",
    purge.ROW_UNKNOWN: "unut.closed",
    # A plan another tap took and has not claimed yet: it is being executed.
    purge.ROW_OPEN: "unut.running",
}


async def _row_text(tap: Tap, purge_log_id: int) -> str:
    async with tap.deps.session_factory() as session:
        return t(ROW_TEXT[await purge.row_state(session, purge_log_id)])


async def on_purge(tap: Tap) -> None:
    purge_log_id = tap.data.ref
    assert purge_log_id is not None
    # Answered first: a purge may outlast the ~15 s a button waits for its
    # answer; the outcome is the edited message.
    await toast(tap.query)
    now = tap.deps.clock()
    plans = tap.deps.plans
    planned = plans.take(PLAN_PURGE, purge_log_id, now=now)
    yes = tap.data.action == "y"
    if planned is None and plans.taken(PLAN_PURGE, purge_log_id, now=now):
        # A second tap (a double tap, ✅ then ❌): the first one holds the
        # plan — this one closes nothing and says where the row stands.
        await edit(tap.query, await _row_text(tap, purge_log_id))
        return
    if not isinstance(planned, purge.PurgePlan) or not yes:
        # A plan this process no longer holds (a restart, ten minutes gone)
        # closes as expired; a row executed or closed already says so.
        why = PURGE_CANCELLED if planned is not None else PURGE_EXPIRED
        async with tap.deps.session_factory() as session:
            closed = await purge.abandon(session, purge_log_id, why=why)
        if not closed:
            await edit(tap.query, await _row_text(tap, purge_log_id))
            return
        said = "unut.cancelled" if why == PURGE_CANCELLED else "unut.expired"
        await edit(tap.query, t(said))
        return
    try:
        async with tap.deps.session_factory() as session:
            result = await purge.execute(
                session,
                planned,
                requested_by=owner.owner_ref(tap.user_id),
                now=now,
            )
    except purge.PurgeRefused as refused:
        await edit(tap.query, t(ROW_TEXT.get(refused.code, "unut.closed")))
        return
    except Exception as exc:
        # A group failed after the ones before it committed (06 §5.4): the
        # owner is told how to finish — /unut plans the company again while
        # it still holds anything (purge.unfinished).
        log.warning("purge stopped", extra={"error": type(exc).__name__})
        # /unut plans a company (plan_company): its root is the one id; a
        # username's plan may name none (plan_handle).
        if planned.company_ids:
            await edit(tap.query, t("unut.failed", id=str(planned.company_ids[0])))
        else:
            await edit(tap.query, t("unut.failed_again"))
        return
    await edit(tap.query, pages.purge_result_text(result, reason=planned.reason))
    if tap.query.bot is not None:
        # The purged lead's cards lose its data now; what Telegram refuses
        # the card job asks again (purge.close_purged_cards).
        await purge.close_purged_cards(
            tap.deps.session_factory, BotCards(tap.query.bot), now=now
        )


# --- /dnc -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DncPlanned:
    arg: str
    key: str


def _shown_key(item: suppression.Entry) -> str:
    return f"@{item.key}" if item.key_type == "tg_username" else item.key


def _exists_text(hit: suppression.Hit) -> str:
    kind = cards.SUPPRESSION_KIND_UZ.get(hit.kind, hit.kind)
    when = cards.day(hit.created_at) if hit.created_at is not None else "—"
    return t("dnc.exists", kind=esc(kind), day=when)


async def dnc(message: Message, deps: Deps) -> None:
    arg = args_of(message)
    try:
        item = suppression.dnc_command_entry(arg)
    except suppression.ForeignPhone:
        await reply(message, t("dnc.foreign"))
        return
    except ValueError:
        await reply(message, t("dnc.how"))
        return
    now = deps.clock()
    async with deps.session_factory() as session:
        held = await suppression.is_suppressed(
            session, [(item.key_type, item.key)], now=now
        )
        names: list[str] = []
        for company_id in await suppression.holders(session, item):
            name = await _name(session, company_id)
            if name is not None and name not in names:
                names.append(name)
    if held is not None and held.permanent:
        await reply(message, _exists_text(held))
        return
    key = _shown_key(item)
    plan_id = deps.plans.put(PLAN_DNC, DncPlanned(arg, key), now=now)
    await reply(
        message,
        pages.dnc_preview_text(key, names),
        keyboard=keyboards.dnc_keyboard(plan_id),
    )


async def _name(session: AsyncSession, company_id: uuid.UUID) -> str | None:
    """The name the owner knows the company by: its root's (a merged
    company's key sits on the root it went into)."""
    found = await session.get(Company, company_id)
    if found is None:
        return None
    return (await companies.root_company(session, found)).display_name


async def on_dnc(tap: Tap) -> None:
    plan_id = tap.data.ref
    assert plan_id is not None
    now = tap.deps.clock()
    planned = tap.deps.plans.take(PLAN_DNC, plan_id, now=now)
    if not isinstance(planned, DncPlanned):
        await edit(tap.query, t("dnc.expired"))
        await toast(tap.query)
        return
    if tap.data.action != "y":
        await edit(tap.query, t("dnc.cancelled"))
        await toast(tap.query)
        return
    async with tap.deps.session_factory() as session:
        result = await suppression.dnc_command(
            session, planned.arg, now=now, reason="owner_command"
        )
    if result.existing is not None:
        await edit(tap.query, _exists_text(result.existing))
    else:
        text = t("dnc.written", key=esc(planned.key))
        if result.suppressed:
            text += "\n" + t("dnc.ended", n=len(result.suppressed))
        await edit(tap.query, text)
    await toast(tap.query)


# --- /vazn ------------------------------------------------------------------------------


def _points(raw: str) -> int | None:
    text = raw.strip().replace("−", "-")
    sign = -1 if text.startswith("-") else 1
    digits = text.lstrip("+-")
    return sign * int(digits) if digits.isascii() and digits.isdigit() else None


async def vazn(message: Message, deps: Deps) -> None:
    args = args_of(message).split()
    if not args:
        async with deps.session_factory() as session:
            weights = await scoring.load_weights(session)
        await reply(message, pages.weights_text(weights))
        return
    if len(args) != 2:
        await reply(message, t("vazn.how"))
        return
    key, raw = args
    points = _points(raw)
    if points is None:
        await reply(message, t("vazn.bad_points"))
        return
    try:
        async with deps.session_factory() as session:
            weights = await scoring.load_weights(session)
            done = await scoring.owner_edit(
                session, key, points, by=_owner(message), now=deps.clock()
            )
    except KeyError:
        await reply(message, t("vazn.unknown", key=esc(key)))
        return
    except scoring.VersionRefused as refused:
        weight = weights.rows.get(key)
        if weight is not None and refused.problems == (f"points:{key}",):
            span = pages.points_range(weight.group, weight.cap)
            await reply(message, t("vazn.refused", key=esc(key), range=span))
        else:
            await reply(
                message, t("vazn.invalid", problems=esc(", ".join(refused.problems)))
            )
        return
    await reply(
        message,
        t(
            "vazn.done",
            key=esc(done.key),
            old=done.old,
            new=done.new,
            version=done.version,
        ),
    )


# --- /push tekshir --------------------------------------------------------------------


async def push(message: Message, deps: Deps) -> None:
    args = args_of(message).split()
    if len(args) != 2 or args[0].lower() != "tekshir":
        await reply(message, t("push.how"))
        return
    async with deps.session_factory() as session:
        check = await crm_push.push_tekshir_view(session, args[1])
    keyboard = (
        keyboards.push_check_keyboard(check.push_log_id)
        if check.refusal is None and check.push_log_id is not None
        else None
    )
    await reply(message, esc(crm_push.render_push_check(check)), keyboard=keyboard)


async def on_push_check(tap: Tap) -> None:
    push_log_id = tap.data.ref
    assert push_log_id is not None
    # Answered first: the re-push may take the ERP's whole retry ladder,
    # longer than the ~15 s a button waits; the outcome is the edited message.
    await toast(tap.query)
    if tap.data.action != "y":
        await edit(tap.query, t("push.cancelled"))
        return
    async with tap.deps.erp() as erp:
        if erp is None:
            await edit(tap.query, t("push.no_erp"))
            return
        async with tap.deps.session_factory() as session:
            result = await crm_push.push_tekshir_confirm(
                session, push_log_id, now=tap.deps.clock(), erp=erp
            )
    if result.status == "sent":
        text = t("push.sent", attempt=result.attempt)
    elif result.status == "unknown":
        text = t("push.unknown", attempt=result.attempt)
    elif result.status == "failed":
        text = t("push.failed", error=esc(result.error or "—"))
    else:
        why = "; ".join(r.text for r in result.refusals) or "—"
        text = t("push.refused", why=esc(why))
    await edit(tap.query, text)


# --- /stir ------------------------------------------------------------------------------


async def stir(message: Message, deps: Deps) -> None:
    args = args_of(message).split()
    if len(args) != 2:
        await reply(message, t("stir.how"))
        return
    candidate_id = cards.parse_uuid(args[0])
    if candidate_id is None:
        await reply(message, t("stir.bad_id"))
        return
    async with deps.session_factory() as session:
        result = await owner.set_stir(session, candidate_id, args[1], now=deps.clock())
    values = {
        "stir": esc(result.stir or ""),
        "name": esc(result.company_name or ""),
        "other": esc(result.other_name or result.other_stir or ""),
    }
    if result.outcome == "has_other":
        values["other"] = esc(result.other_stir or "")
    await reply(message, t(f"stir.{result.outcome}", **values))


__all__ = [
    "IMPORT_MAX_BYTES",
    "dnc",
    "import_command",
    "on_clear_crm",
    "on_dnc",
    "on_import",
    "on_keep",
    "on_purge",
    "on_push_check",
    "on_resume",
    "push",
    "stir",
    "toxtat",
    "unut",
    "vazn",
    "yoq",
]
