"""Contact rows that outlive their person (02 §4 R4/R5, 04 §2.4).

``anonymise`` is the one way a person-tier row stops being about a person:
the resolver calls it when a better decision maker replaces the company's
one person (``ux_contacts_one_person``), retention R4/R5 when a lead went
unanswered or its company was excluded. The row stays — signals, candidates
and observations keep pointing at it — and only the person goes: name, role,
the personal Telegram id and handle, Instagram, e-mail, and a mobile phone. A
landline stays, because a landline is the company's switchboard, not the
person's (02 §4 R4's SQL: ``phone := NULL WHERE tier='person' AND
phone_kind='mobile'``).
"""

from __future__ import annotations

from datetime import datetime

from clientscrapper.db.models import Contact


def anonymise(contact: Contact, *, now: datetime) -> None:
    """Strip the person from ``contact`` in place (flushed by the caller).
    Idempotent: an anonymised row keeps its first ``anonymised_at``."""
    contact.full_name = None
    contact.role = None
    contact.telegram_id = None
    contact.telegram_username = None
    contact.instagram = None
    contact.email = None
    if contact.tier == "person" and contact.phone_kind == "mobile":
        # Both columns go together (ck_contacts_phone_kind).
        contact.phone = None
        contact.phone_kind = None
    if contact.anonymised_at is None:
        contact.anonymised_at = now
