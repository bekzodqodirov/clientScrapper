"""Companies, their aliases, external ids and contacts (02 §2.4).

Person-tier data lives only in ``contacts`` (06 §5.1): at most one live
person-tier row per company, and none without its legal basis, source and
collection time.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from clientscrapper.db import enums as e
from clientscrapper.db.base import Base, created_at_column, updated_at_column
from clientscrapper.db.models._common import (
    EMPTY_ARRAY,
    UUID_DEFAULT,
    bigint_pk,
    check_in,
)


class Company(Base):
    """The unit the agent finds, scores and pushes; ``external_id`` embeds its id.

    A person-shaped name never becomes ``display_name`` (an OLX seller is
    «OLX sotuvchi <id>» plus one person-tier contact). The normalised keys are
    held to their SQL mirrors by CHECK, so a raw value cannot be stored.
    """

    __tablename__ = "companies"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=UUID_DEFAULT
    )
    display_name: Mapped[str] = mapped_column(sa.Text)
    name_key: Mapped[str] = mapped_column(sa.Text)
    legal_form: Mapped[str | None] = mapped_column(sa.Text)
    stir: Mapped[str | None] = mapped_column(sa.CHAR(9))
    oked: Mapped[str | None] = mapped_column(sa.CHAR(5))
    city: Mapped[str | None] = mapped_column(sa.Text)
    region: Mapped[str | None] = mapped_column(sa.Text)
    address: Mapped[str | None] = mapped_column(sa.Text)
    website_domain: Mapped[str | None] = mapped_column(sa.Text)
    # The @company channel, not a person's handle.
    telegram_channel: Mapped[str | None] = mapped_column(sa.Text)
    # The company's Instagram account.
    instagram: Mapped[str | None] = mapped_column(sa.Text)
    registry_status: Mapped[str] = mapped_column(sa.Text, server_default="unknown")
    registered_on: Mapped[date | None] = mapped_column(sa.Date)
    sectors: Mapped[list[str]] = mapped_column(ARRAY(sa.Text), server_default=EMPTY_ARRAY)
    # Uzbek, 1–6 items, from ImportSignal.
    goods: Mapped[list[str]] = mapped_column(ARRAY(sa.Text), server_default=EMPTY_ARRAY)
    # A bazaar wholesaler … X competitor … Z unclear (namuna-nomzodlar).
    segment: Mapped[str | None] = mapped_column(sa.CHAR(1))
    volume_band: Mapped[str] = mapped_column(sa.Text, server_default="unknown")
    is_competitor: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    is_supplier_cn: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    is_state_body: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    state: Mapped[str] = mapped_column(sa.Text, server_default="new")
    exclude_reason: Mapped[str | None] = mapped_column(sa.Text)
    merged_into: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="SET NULL")
    )
    # The resolver's 80–89 fuzzy band: the «🔀 Birlashtir» proposal (04 §2.2).
    dup_of: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="SET NULL")
    )
    # No CHECK pairs it with dup_of: ON DELETE SET NULL clears dup_of alone.
    dup_score: Mapped[int | None] = mapped_column(sa.SmallInteger)
    # The last RelevanceVerdict (model output only).
    verdict: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    verdict_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = created_at_column()
    last_signal_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    best_score: Mapped[int | None] = mapped_column(sa.SmallInteger)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        sa.CheckConstraint(
            "length(display_name) BETWEEN 2 AND 160", name="ck_companies_display_name"
        ),
        sa.CheckConstraint(
            "length(name_key) BETWEEN 1 AND 160", name="ck_companies_name_key"
        ),
        check_in("companies", "legal_form", e.LEGAL_FORMS),
        sa.CheckConstraint("stir ~ '^[0-9]{9}$'", name="ck_companies_stir"),
        sa.CheckConstraint("oked ~ '^[0-9]{5}$'", name="ck_companies_oked"),
        sa.CheckConstraint("length(address) <= 300", name="ck_companies_address"),
        sa.CheckConstraint(
            "website_domain = cs_domain_key(website_domain)",
            name="ck_companies_website_domain",
        ),
        sa.CheckConstraint(
            "telegram_channel = cs_username_key(telegram_channel)",
            name="ck_companies_telegram_channel",
        ),
        sa.CheckConstraint("instagram = lower(instagram)", name="ck_companies_instagram"),
        check_in("companies", "registry_status", e.REGISTRY_STATUSES),
        sa.CheckConstraint("segment ~ '^[A-Z]$'", name="ck_companies_segment"),
        check_in("companies", "volume_band", e.VOLUME_BANDS),
        check_in("companies", "state", e.COMPANY_STATES),
        sa.CheckConstraint("dup_score BETWEEN 0 AND 100", name="ck_companies_dup_score"),
        sa.CheckConstraint(
            "best_score BETWEEN 0 AND 100", name="ck_companies_best_score"
        ),
        sa.CheckConstraint(
            "(state = 'merged') = (merged_into IS NOT NULL)", name="ck_companies_merged"
        ),
        sa.CheckConstraint(
            "state <> 'excluded' OR exclude_reason IS NOT NULL",
            name="ck_companies_excluded",
        ),
        sa.Index(
            "ux_companies_stir",
            "stir",
            unique=True,
            postgresql_where=sa.text("stir IS NOT NULL AND state <> 'merged'"),
        ),
        sa.Index(
            "ux_companies_domain",
            "website_domain",
            unique=True,
            postgresql_where=sa.text("website_domain IS NOT NULL AND state <> 'merged'"),
        ),
        sa.Index(
            "ix_companies_name_key_trgm",
            "name_key",
            postgresql_using="gin",
            postgresql_ops={"name_key": "gin_trgm_ops"},
        ),
        sa.Index("ix_companies_state_signal", "state", sa.text("last_signal_at DESC")),
        sa.Index("ix_companies_merged_into", "merged_into"),
        sa.Index(
            "ix_companies_dup_of",
            "dup_of",
            postgresql_where=sa.text("dup_of IS NOT NULL"),
        ),
    )


class CompanyAlias(Base):
    """Every name a company was seen under; ``alias_key`` is ``name_key`` of it."""

    __tablename__ = "company_aliases"

    id: Mapped[int] = bigint_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="CASCADE")
    )
    alias: Mapped[str] = mapped_column(sa.Text)
    alias_key: Mapped[str] = mapped_column(sa.Text)
    source_key: Mapped[str | None] = mapped_column(sa.Text, sa.ForeignKey("sources.key"))
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        sa.CheckConstraint(
            "length(alias) BETWEEN 2 AND 160", name="ck_company_aliases_alias"
        ),
        sa.UniqueConstraint(
            "company_id", "alias_key", name="uq_company_aliases_company_key"
        ),
        sa.Index(
            "ix_company_aliases_key_trgm",
            "alias_key",
            postgresql_using="gin",
            postgresql_ops={"alias_key": "gin_trgm_ops"},
        ),
        sa.Index("ix_company_aliases_key", "alias_key"),
    )


class CompanyId(Base):
    """A platform's id for a company (hh employer, 2GIS firm, place_id …).
    ``google_place`` is the only Places value kept past 30 days."""

    __tablename__ = "company_ids"

    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="CASCADE")
    )
    id_type: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    id_value: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    source_key: Mapped[str | None] = mapped_column(sa.Text, sa.ForeignKey("sources.key"))
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        check_in("company_ids", "id_type", e.ID_TYPES),
        sa.CheckConstraint(
            "length(id_value) BETWEEN 1 AND 200", name="ck_company_ids_id_value"
        ),
        sa.Index("ix_company_ids_company", "company_id"),
    )


class Contact(Base):
    """A way to reach a company. Company-tier rows hold switchboard phones,
    ``info@`` and the @company handle; a person-tier row is the one decision
    maker, replaced (old row anonymised, never deleted) by a second."""

    __tablename__ = "contacts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=UUID_DEFAULT
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="CASCADE")
    )
    tier: Mapped[str] = mapped_column(sa.Text)
    full_name: Mapped[str | None] = mapped_column(sa.Text)
    role: Mapped[str | None] = mapped_column(sa.Text)
    phone: Mapped[str | None] = mapped_column(sa.Text)
    # The key every phone lookup and the ERP's suppression hash use.
    phone_tail: Mapped[str | None] = mapped_column(
        sa.CHAR(9), sa.Computed("cs_phone_tail(phone)", persisted=True)
    )
    phone_kind: Mapped[str | None] = mapped_column(sa.Text)
    telegram_username: Mapped[str | None] = mapped_column(sa.Text)
    # A poster's personal Telegram id: person-tier, never in company_ids.
    telegram_id: Mapped[int | None] = mapped_column(sa.BigInteger)
    instagram: Mapped[str | None] = mapped_column(sa.Text)
    email: Mapped[str | None] = mapped_column(sa.Text)
    is_decision_maker: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    # The same phone on >= 3 unrelated companies (an accountant, a broker).
    shared_phone: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    legal_basis: Mapped[str | None] = mapped_column(sa.Text)
    source_url: Mapped[str | None] = mapped_column(sa.Text)
    source_key: Mapped[str | None] = mapped_column(sa.Text, sa.ForeignKey("sources.key"))
    collected_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    anonymised_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        check_in("contacts", "tier", e.CONTACT_TIERS),
        sa.CheckConstraint("length(full_name) <= 120", name="ck_contacts_full_name"),
        sa.CheckConstraint("length(role) <= 80", name="ck_contacts_role"),
        sa.CheckConstraint(r"phone ~ '^\+[1-9][0-9]{6,14}$'", name="ck_contacts_phone"),
        # ck_contacts_phone_kind is the pairing rule below (02 names it so),
        # hence the value list's own suffix.
        sa.CheckConstraint(
            f"phone_kind IN ({e.sql_list(e.PHONE_KINDS)})",
            name="ck_contacts_phone_kind_values",
        ),
        sa.CheckConstraint(
            "telegram_username = cs_username_key(telegram_username)",
            name="ck_contacts_telegram_username",
        ),
        sa.CheckConstraint("instagram = lower(instagram)", name="ck_contacts_instagram"),
        sa.CheckConstraint(
            "email ~ '^[^@[:space:]]+@[^@[:space:]]+$'", name="ck_contacts_email"
        ),
        check_in("contacts", "legal_basis", e.LEGAL_BASES),
        sa.CheckConstraint("length(source_url) <= 2048", name="ck_contacts_source_url"),
        sa.CheckConstraint(
            "tier = 'company' OR anonymised_at IS NOT NULL OR (legal_basis IS NOT NULL "
            "AND source_url IS NOT NULL AND collected_at IS NOT NULL)",
            name="ck_contacts_person_basis",
        ),
        sa.CheckConstraint(
            "tier = 'person' "
            "OR (full_name IS NULL AND role IS NULL AND telegram_id IS NULL)",
            name="ck_contacts_company_no_name",
        ),
        sa.CheckConstraint(
            "(phone IS NULL) = (phone_kind IS NULL)", name="ck_contacts_phone_kind"
        ),
        sa.CheckConstraint(
            "coalesce(full_name, '') !~ '[0-9]{14}' "
            "AND coalesce(role, '') !~ '[0-9]{14}'",
            name="ck_contacts_no_pinfl",
        ),
        sa.Index(
            "ux_contacts_one_person",
            "company_id",
            unique=True,
            postgresql_where=sa.text("tier = 'person' AND anonymised_at IS NULL"),
        ),
        sa.Index(
            "ux_contacts_company_phone",
            "company_id",
            "phone",
            unique=True,
            postgresql_where=sa.text("phone IS NOT NULL"),
        ),
        sa.Index("ix_contacts_company", "company_id"),
        sa.Index(
            "ix_contacts_phone_tail",
            "phone_tail",
            postgresql_where=sa.text("phone_tail IS NOT NULL"),
        ),
        sa.Index(
            "ix_contacts_tg_username",
            "telegram_username",
            postgresql_where=sa.text("telegram_username IS NOT NULL"),
        ),
        sa.Index(
            "ix_contacts_tg_id",
            "telegram_id",
            postgresql_where=sa.text("telegram_id IS NOT NULL"),
        ),
        sa.Index(
            "ix_contacts_instagram",
            "instagram",
            postgresql_where=sa.text("instagram IS NOT NULL"),
        ),
    )
