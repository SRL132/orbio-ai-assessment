"""Retention: erase candidates after the client's retention periods. Disqualified and abandoned
have one by default. Qualified, needs-review and on-file candidates are kept unless the config sets
theirs, which counsel decides.

The policy is in docs/process-design-appendix.md ("Retention"). A candidate who agreed to stay
on file is kept. Erasure removes everything that can hold personal data (state, messages,
events including recruiter notes, interview bookings, the WhatsApp contact) in one
transaction, and leaves an `erasures` row with nothing of theirs as proof it happened.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import Engine, and_, or_, select
from sqlalchemy.orm import Session

from app.adapters.storage.sql import ConversationRow, erase_conversations
from app.domain.config import ClientConfig
from app.domain.models import Status
from app.domain.ports import Clock

logger = logging.getLogger("app.retention")

ERASABLE = (Status.DISQUALIFIED.value, Status.ABANDONED.value)
_BATCH = 200


class SqlRetention:
    def __init__(self, engine: Engine, config: ClientConfig, clock: Clock) -> None:
        self._engine = engine
        self._config = config
        self._clock = clock

    def erase_expired(self) -> int:
        """Erase every candidate past the retention period. Returns how many were erased."""
        erased = 0
        while batch := self._erase_batch():
            erased += batch
        return erased

    def _erase_batch(self) -> int:
        now = self._clock.now()
        cutoff = now - timedelta(days=self._config.retention_days)
        conv = ConversationRow
        with Session(self._engine) as session, session.begin():
            not_on_file = or_(conv.keep_on_file.is_(None), conv.keep_on_file.is_(False))
            extra = [
                and_(
                    conv.status == status.value,
                    conv.updated_at < now - timedelta(days=days),
                    not_on_file,
                )
                for status, days in self._config.retention_days_by_status.items()
            ]
            if (on_file := self._config.retention_days_on_file) is not None:
                extra.append(
                    and_(
                        conv.keep_on_file.is_(True),
                        conv.updated_at < now - timedelta(days=on_file),
                    )
                )
            expired = session.execute(
                select(conv.id, conv.status, conv.updated_at)
                .where(
                    conv.client_id == self._config.client_id,
                    or_(
                        and_(
                            conv.updated_at < cutoff,  # last activity: a late reply restarts it
                            or_(
                                conv.invite_pending.is_(True),  # expired long ago, never opened
                                # Staying on file for future roles is a separate consent: kept.
                                and_(conv.status.in_(ERASABLE), not_on_file),
                            ),
                        ),
                        *extra,
                    ),
                )
                .limit(_BATCH)
            ).all()
            ids = [row.id for row in expired]
            if not ids:
                return 0
            erase_conversations(session, self._config.client_id, expired, now)
        logger.info("retention_erased count=%d", len(ids))
        return len(ids)
