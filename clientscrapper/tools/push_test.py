"""Push one visibly synthetic lead to the ERP (05 §B.1, §A.14 step 4, 07 §5.6).

    make push-test
    python -m clientscrapper.tools.push_test

Mints a FRESH synthetic company every run — a new uuid, so a new
``external_id`` ``cs:<uuid>:<yyyymm>`` (a fixed id would be a silent replay on
a month's second run) — named «TEST agent — oʻchiring», with a visibly
synthetic value for every optional key of ``PushPayload`` (``kub: "1"`` as a
stated volume, STIR ``300000099``, ``namuna_test``, ``test.example.uz``, an
address, ``imports``, ``evidence``): every key then arrives once and shows on
the ERP's «Reklamadan kelgan yangi savollar» list, which lists only keys of the
last 30 days. One company-tier contact with ``TEST_PHONE``, one stated-volume
signal on its own ``manual`` evidence, an ``approved`` candidate
(``approved_by='owner:push-test'``, ``score=0``, ``score_breakdown.gates.test``
— no scorer, card or ``crm_push_job`` ever touches it) — pushed through
``crm_push.push_one`` like every push, with exactly one item of the checklist
skipped: 04 §7 item 13 (the score; gate 1 ``is_b2b`` is never the push gate's).
Suppression, freshness, the breaker, the window, ``phone_recent`` and the caps
all hold. A refused push rolls the synthetic company back with it.

``TEST_PHONE`` must be outside the client book and every «Qayta qilinmasin»
lead, or the ERP lands the push silently as ``dropped/suppressed`` (still
200): the tool refuses to run when the local gate already suppresses it. The
STIR and the domain are unique per live company, so an earlier run's TEST
company gives them up first (only a company with the TEST name; any other
holder is a refusal).

Prints the status, the HTTP status, the ``external_id`` and the company uuid
(for ``tools/purge``). Exit codes: 0 the ERP recorded it (``sent``), 1 it
failed or the answer is unknown, 2 refused before anything was sent.
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from clientscrapper.collectors.runner import SessionFactory
from clientscrapper.config import settings
from clientscrapper.db.models import Company, Contact, Evidence, LeadCandidate, Signal
from clientscrapper.db.session import SessionLocal
from clientscrapper.services import crm_push, erp_feeds, keys, scoring, suppression
from clientscrapper.services.erp_feeds import ErpClient

EXIT_SENT: Final = 0
EXIT_FAILED: Final = 1
EXIT_REFUSED: Final = 2

TEST_NAME: Final = "TEST agent — oʻchiring"
TEST_LEGAL_FORM: Final = "MChJ"
TEST_STIR: Final = "300000099"
TEST_HANDLE: Final = "namuna_test"
TEST_DOMAIN: Final = "test.example.uz"
TEST_CITY: Final = "Toshkent"
TEST_ADDRESS: Final = "Namuna koʻchasi 1, Toshkent (sinov manzili)"
TEST_GOODS: Final = ("namuna tovar", "aksessuar")
TEST_SECTORS: Final = ("sinov",)
TEST_STATED_M3: Final = Decimal(1)
TEST_QUOTE: Final = "1 kub namuna tovar"
TEST_EXCERPT: Final = (
    "Sinov arizasi (make push-test): 1 kub namuna tovar Xitoydan. Bu lid sinov "
    "uchun — oʻchiring."
)
TEST_SOURCE: Final = "manual"
APPROVED_BY: Final = "owner:push-test"
EVIDENCE_KEPT: Final = timedelta(days=180)


@dataclass(slots=True)
class PushTestReport:
    status: str
    company_id: uuid.UUID | None = None
    external_id: str | None = None
    http_status: int | None = None
    error: str | None = None
    refusals: list[str] = field(default_factory=list)
    reason: str | None = None

    def lines(self) -> list[str]:
        if self.status == "refused":
            why = self.reason or "; ".join(self.refusals)
            return [f"push-test: rad etildi — {why}"]
        return [
            f"push-test: {self.status} · HTTP {self.http_status or '—'}"
            + (f" · {self.error}" if self.error else ""),
            f"external_id: {self.external_id}",
            f"company: {self.company_id}",
        ]

    def exit_code(self) -> int:
        if self.status == "sent":
            return EXIT_SENT
        if self.status in ("unknown", "failed"):
            return EXIT_FAILED
        return EXIT_REFUSED


def test_phone() -> str | None:
    """``TEST_PHONE`` as E.164, when it is an Uzbek mobile or landline."""
    phone = keys.phone_e164(settings.test_phone or None)
    if phone is None or keys.phone_kind(phone) not in scoring.USABLE_PHONE_KINDS:
        return None
    return phone


async def suppressed(session: AsyncSession, phone: str, *, now: datetime) -> bool:
    """The local gate on ``phone``'s tail (and its hash under the key): a hit
    would land the push silently as ``dropped/suppressed``."""
    tail = keys.phone_tail(phone)
    found = suppression.build_keys(
        [], [], settings.crm_agent_key, tails=[tail] if tail else []
    )
    return await suppression.is_suppressed(session, found, now=now) is not None


async def free_synthetic_keys(session: AsyncSession) -> str | None:
    """Earlier TEST companies give up the STIR and the domain (unique per live
    company); None, or why a holder that is not a TEST company refuses."""
    holders = (
        await session.execute(
            sa.select(Company.id, Company.display_name).where(
                sa.or_(Company.stir == TEST_STIR, Company.website_domain == TEST_DOMAIN),
                Company.state != "merged",
            )
        )
    ).all()
    foreign = [h for h in holders if h.display_name != TEST_NAME]
    if foreign:
        return f"STIR {TEST_STIR} yoki {TEST_DOMAIN} boshqa kompaniyada"
    if holders:
        await session.execute(
            sa.update(Company)
            .where(Company.id.in_([h.id for h in holders]))
            .values(stir=None, website_domain=None)
        )
    return None


async def mint(
    session: AsyncSession, phone: str, *, now: datetime
) -> tuple[Company, LeadCandidate]:
    """The synthetic company, its contact, evidence, stated-volume signal and
    approved candidate — flushed, not committed (``push_one`` commits them
    with its claim, or the refusal rolls them back)."""
    company = Company(
        id=uuid.uuid4(),
        display_name=TEST_NAME,
        name_key=keys.name_key(TEST_NAME),
        legal_form=TEST_LEGAL_FORM,
        stir=TEST_STIR,
        city=TEST_CITY,
        address=TEST_ADDRESS,
        website_domain=TEST_DOMAIN,
        telegram_channel=TEST_HANDLE,
        instagram=TEST_HANDLE,
        registry_status="active",
        sectors=list(TEST_SECTORS),
        goods=list(TEST_GOODS),
        volume_band="1_3",
        state="active",
        last_signal_at=now,
    )
    session.add(company)
    await session.flush()
    contact = Contact(
        company_id=company.id,
        tier="company",
        phone=phone,
        phone_kind=keys.phone_kind(phone),
        source_key=TEST_SOURCE,
        collected_at=now,
    )
    url = f"https://{TEST_DOMAIN}/push-test/{company.id}"
    evidence = Evidence(
        source_key=TEST_SOURCE,
        url=url,
        fetched_at=now,
        published_at=now,
        sha256=hashlib.sha256(f"push-test:{company.id}".encode()).hexdigest(),
        bytes=len(TEST_EXCERPT.encode()),
        excerpt=TEST_EXCERPT,
        expires_at=now + EVIDENCE_KEPT,
    )
    session.add_all([contact, evidence])
    await session.flush()
    session.add(
        Signal(
            company_id=company.id,
            evidence_id=evidence.id,
            key=scoring.STATED_1_3,
            value=TEST_STATED_M3,
            quote=TEST_QUOTE,
            observed_at=now,
            source_key=TEST_SOURCE,
        )
    )
    candidate = LeadCandidate(
        company_id=company.id,
        contact_id=contact.id,
        state="approved",
        tier="cold",
        score=0,
        score_breakdown={"gates": {"test": True}},
        approved_by=APPROVED_BY,
        approved_at=now,
        last_scored_at=now,
    )
    session.add(candidate)
    await session.flush()
    return company, candidate


async def run(
    *,
    now: datetime,
    erp: ErpClient,
    session_factory: SessionFactory = SessionLocal,
) -> PushTestReport:
    """One synthetic push (see the module docstring)."""
    phone = test_phone()
    if phone is None:
        return PushTestReport(
            "refused",
            reason="TEST_PHONE .env da yoʻq yoki Oʻzbekiston mobil/shahar raqami emas",
        )
    async with session_factory() as session:
        if await suppressed(session, phone, now=now):
            return PushTestReport(
                "refused",
                reason="TEST_PHONE bostirilgan (mijoz yoki «Qayta qilinmasin»): "
                "boshqa raqam tanlang",
            )
        taken = await free_synthetic_keys(session)
        if taken is not None:
            await session.rollback()
            return PushTestReport("refused", reason=taken)
        company, candidate = await mint(session, phone, now=now)
        company_id = company.id
        result = await crm_push.push_one(
            session, candidate.id, now=now, erp=erp, synthetic=True
        )
    if result.status == "refused":
        return PushTestReport(
            "refused", refusals=[r.text for r in result.refusals] or result.codes
        )
    return PushTestReport(
        result.status,
        company_id=company_id,
        external_id=result.external_id,
        http_status=result.http_status,
        error=result.error,
    )


async def _main() -> int:
    try:
        erp = erp_feeds.client_from_settings()
    except ValueError as exc:
        print(f"push-test: ERP sozlanmagan — {exc}")
        return EXIT_REFUSED
    async with erp:
        report = await run(now=datetime.now(settings.tz), erp=erp)
    for line in report.lines():
        print(line)
    return report.exit_code()


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    return asyncio.run(_main())


if __name__ == "__main__":
    sys.exit(main())
