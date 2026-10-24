"""Person-tier data has a fixed set of homes (02 §0, 06 §5.1).

Any column whose name looks like a phone, a name, a username, a bio or an
e-mail must be in ``ALLOWED`` with the reason it may exist; a new one is red
until somebody writes that reason down (and its retention step, 02 §4).
No database: walks ``Base.metadata``.
"""

from __future__ import annotations

import re

from clientscrapper.db import models as _models  # noqa: F401 - populates Base.metadata
from clientscrapper.db.base import Base

PII_NAME = re.compile(r"phone|name|username|bio|email")

ALLOWED: dict[str, str] = {
    # Company tier: names of firms, channels and ERP stages, not people.
    "companies.display_name": "company name; a person-shaped name is never stored here",
    "companies.name_key": "normalised company name",
    "chat_monitors.username": "public @username of a group or channel",
    "outcomes.stage_name": "ERP funnel stage label",
    # Person tier: contacts is the one home (one live row per company).
    "contacts.full_name": "the one decision maker; legal_basis required; R4/R5 anonymise",
    "contacts.phone": "switchboard or decision maker phone; R4 nulls a person's mobile",
    "contacts.phone_tail": "generated from phone; the suppression and dedupe key",
    "contacts.phone_kind": "a category, not a number",
    "contacts.shared_phone": "a flag (same phone on >= 3 companies), not a number",
    "contacts.telegram_username": "@company channel or the decision maker's handle; R4",
    "contacts.email": "info@ or the decision maker's; R4",
    # Transient: Telegram posts (R2a deletes at 30 d, R2b nulls at 90 d).
    "interactions.sender_username": "poster's handle; R2a/R2b",
    "interactions.sender_name": "poster's display name; R2b nulls it",
    "interactions.sender_bio": "poster's bio (tout rule); R2b nulls it",
    # Ledgers keep only the 9-digit tail, never a whole number.
    "push_log.phone_tail": "the tail a push used: 24 h per-phone gate and suppression",
}


def _pii_columns() -> set[str]:
    return {
        f"{table.name}.{column.name}"
        for table in Base.metadata.tables.values()
        for column in table.columns
        if PII_NAME.search(column.name)
    }


def test_every_pii_shaped_column_is_allowed_with_a_reason():
    assert sorted(_pii_columns() - set(ALLOWED)) == []


def test_the_allowlist_names_only_columns_that_exist():
    assert sorted(set(ALLOWED) - _pii_columns()) == []
