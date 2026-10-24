"""ORM models (02 §6). Every model is re-exported here; this is the only
import alembic/env.py needs to populate ``Base.metadata``.

Model conventions (MIYA): ``Mapped[...]`` annotations, JSONB typed
``dict[str, Any]``, ``Decimal`` for numeric, every constraint and index named
in ``__table_args__``. No relationships yet: a later task that needs one
declares it ``lazy="raise"``. State transitions live in services, not here.
"""

from clientscrapper.db.models.companies import Company, CompanyAlias, CompanyId, Contact
from clientscrapper.db.models.evidence import Evidence, Observation
from clientscrapper.db.models.leads import LeadCandidate, LeadReview, Outcome, PushLog
from clientscrapper.db.models.ops import (
    Heartbeat,
    MergeLog,
    ModelJob,
    PurgeLog,
    ReminderLog,
    RuntimeFlag,
    SheetRow,
    UsageLog,
)
from clientscrapper.db.models.signals import ScoringWeight, Signal
from clientscrapper.db.models.sources import CollectorRun, Source
from clientscrapper.db.models.suppression import Suppression
from clientscrapper.db.models.telegram import ChatMonitor, Interaction

__all__ = [
    "ChatMonitor",
    "CollectorRun",
    "Company",
    "CompanyAlias",
    "CompanyId",
    "Contact",
    "Evidence",
    "Heartbeat",
    "Interaction",
    "LeadCandidate",
    "LeadReview",
    "MergeLog",
    "ModelJob",
    "Observation",
    "Outcome",
    "PurgeLog",
    "PushLog",
    "ReminderLog",
    "RuntimeFlag",
    "ScoringWeight",
    "SheetRow",
    "Signal",
    "Source",
    "Suppression",
    "UsageLog",
]
