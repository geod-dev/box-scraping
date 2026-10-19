"""Persisted notifications (read/unread), written by the worker and readable through the API."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from competitor_catalog.db.models import Notification

log = logging.getLogger(__name__)


def notify(
    session: Session,
    *,
    level: str,
    kind: str,
    title: str,
    message: str | None = None,
    competitor_id: int | None = None,
    job_id: int | None = None,
    run_id: int | None = None,
    data: dict[str, Any] | None = None,
) -> Notification:
    notification = Notification(
        level=level,
        kind=kind,
        title=title,
        message=message,
        competitor_id=competitor_id,
        job_id=job_id,
        scrape_run_id=run_id,
        data=data or {},
    )
    session.add(notification)
    session.flush()
    getattr(log, "error" if level == "error" else "warning" if level == "warning" else "info")(
        "notification [%s] %s%s", kind, title, f": {message}" if message else ""
    )
    return notification
