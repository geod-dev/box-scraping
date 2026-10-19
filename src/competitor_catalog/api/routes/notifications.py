"""Notifications (job lifecycle and errors), persisted with a read/unread state."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import func, select, update

from competitor_catalog.api import schemas, views
from competitor_catalog.api.deps import PageDep, SessionDep, competitor_id_filter, not_found, require_api_key
from competitor_catalog.db.models import Notification
from competitor_catalog.services.runs import utcnow

router = APIRouter(prefix="/notifications", tags=["notifications"], dependencies=[Depends(require_api_key)])


def _notification(session, notification_id: int) -> Notification:
    notification = session.get(Notification, notification_id)
    if notification is None:
        raise not_found("notification", notification_id)
    return notification


def _filters(stmt, session, *, unread, level, kind, competitor, job_id, since_id):
    if unread is True:
        stmt = stmt.where(Notification.read_at.is_(None))
    elif unread is False:
        stmt = stmt.where(Notification.read_at.is_not(None))
    if level:
        stmt = stmt.where(Notification.level.in_(level))
    if kind:
        stmt = stmt.where(Notification.kind.in_(kind))
    competitor_id = competitor_id_filter(session, competitor)
    if competitor_id is not None:
        stmt = stmt.where(Notification.competitor_id == competitor_id)
    if job_id is not None:
        stmt = stmt.where(Notification.job_id == job_id)
    if since_id is not None:
        stmt = stmt.where(Notification.id > since_id)
    return stmt


@router.get("", response_model=schemas.Page[schemas.NotificationOut], summary="List notifications, newest first")
def list_notifications(
    session: SessionDep,
    page: PageDep,
    unread: Annotated[bool | None, Query(description="true = unread only, false = read only")] = None,
    level: Annotated[list[schemas.NotificationLevel] | None, Query(description="repeatable")] = None,
    kind: Annotated[list[str] | None, Query(description="repeatable, e.g. job_failed")] = None,
    competitor: str | None = None,
    job_id: int | None = None,
    since_id: Annotated[int | None, Query(description="only notifications with a greater id (polling)")] = None,
):
    stmt = _filters(
        select(Notification).order_by(Notification.id.desc()),
        session,
        unread=unread,
        level=level,
        kind=kind,
        competitor=competitor,
        job_id=job_id,
        since_id=since_id,
    )
    result = page.page(session, stmt)
    result["items"] = [views.notification_out(n) for n in result["items"]]
    return result


@router.get("/unread-count", response_model=schemas.UnreadCount, summary="Number of unread notifications")
def unread_count(session: SessionDep, competitor: str | None = None):
    stmt = select(Notification.level, func.count()).where(Notification.read_at.is_(None)).group_by(Notification.level)
    competitor_id = competitor_id_filter(session, competitor)
    if competitor_id is not None:
        stmt = stmt.where(Notification.competitor_id == competitor_id)
    by_level = dict(session.execute(stmt).all())
    return schemas.UnreadCount(unread=sum(by_level.values()), by_level=by_level)


@router.post("/read-all", response_model=schemas.MarkedCount, summary="Mark notifications as read")
def read_all(
    session: SessionDep,
    competitor: str | None = None,
    level: Annotated[list[schemas.NotificationLevel] | None, Query()] = None,
    up_to_id: Annotated[int | None, Query(description="only notifications with id <= this")] = None,
):
    stmt = update(Notification).where(Notification.read_at.is_(None))
    competitor_id = competitor_id_filter(session, competitor)
    if competitor_id is not None:
        stmt = stmt.where(Notification.competitor_id == competitor_id)
    if level:
        stmt = stmt.where(Notification.level.in_(level))
    if up_to_id is not None:
        stmt = stmt.where(Notification.id <= up_to_id)
    result = session.execute(stmt.values(read_at=utcnow()))
    session.commit()
    return schemas.MarkedCount(updated=result.rowcount or 0)


@router.get("/{notification_id}", response_model=schemas.NotificationOut, summary="One notification")
def get_notification(notification_id: int, session: SessionDep):
    return views.notification_out(_notification(session, notification_id))


@router.post("/{notification_id}/read", response_model=schemas.NotificationOut, summary="Mark as read")
def mark_read(notification_id: int, session: SessionDep):
    notification = _notification(session, notification_id)
    notification.read_at = notification.read_at or utcnow()
    session.commit()
    return views.notification_out(notification)


@router.post("/{notification_id}/unread", response_model=schemas.NotificationOut, summary="Mark as unread")
def mark_unread(notification_id: int, session: SessionDep):
    notification = _notification(session, notification_id)
    notification.read_at = None
    session.commit()
    return views.notification_out(notification)


@router.delete("/{notification_id}", status_code=204, summary="Delete a notification")
def delete_notification(notification_id: int, session: SessionDep):
    session.delete(_notification(session, notification_id))
    session.commit()
    return Response(status_code=204)
