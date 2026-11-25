"""Timezone-aware scheduler that keeps pursuing each workspace's daily target."""
import json
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import redis
from sqlalchemy import select

from .config import settings
from .db import SessionLocal
from .models import Notification, OutreachJob, User, Subscription, Workspace
from .ops import enqueue_job, kill_switch_enabled


def _active_daily_job(db, workspace_id: int) -> bool:
    return bool(db.scalar(
        select(OutreachJob.id).where(
            OutreachJob.workspace_id == workspace_id,
            OutreachJob.job_type == "daily_outreach",
            OutreachJob.status.in_(("queued", "running")),
        )
    ))


def _remaining_daily_slots(db, workspace_id: int) -> int:
    # Keep the scheduler and sender on the same daily limit, plan limit and
    # timezone calculation without creating an import cycle at module load.
    from .main import remaining_daily_send_budget
    return remaining_daily_send_budget(db, workspace_id)


def run() -> None:
    client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    while True:
        try:
            with SessionLocal() as db:
                workspaces = db.scalars(select(Workspace).where(Workspace.deleted_at.is_(None), Workspace.paused.is_(False))).all()
                for workspace in workspaces:
                    try:
                        local = datetime.now(ZoneInfo(workspace.timezone))
                        schedule_hour, schedule_minute = (int(item) for item in workspace.schedule_time.split(":", 1))
                        scheduled_at = local.replace(hour=schedule_hour, minute=schedule_minute, second=0, microsecond=0)
                    except Exception:
                        continue
                    if local < scheduled_at:
                        continue
                    subscription = db.scalar(select(Subscription).where(Subscription.workspace_id == workspace.id).order_by(Subscription.id.desc()))
                    if not subscription or subscription.status not in {"active", "trialing", "past_due"}:
                        continue
                    if local.strftime("%H:%M") == workspace.schedule_time and subscription.status == "trialing" and subscription.trial_ends_at:
                        days_left = (subscription.trial_ends_at.date() - local.date()).days
                        if days_left in {5, 2, 0}:
                            reminder_key = f"tailormail:trial-reminder:{workspace.id}:{days_left}:{local.date().isoformat()}"
                            owner = db.get(User, workspace.owner_user_id)
                            if owner and client.set(reminder_key, "1", nx=True, ex=172800):
                                enqueue_job("trial_reminder", {"email": owner.email, "days_left": days_left}, workspace.id, owner.id)
                                db.add(Notification(workspace_id=workspace.id, user_id=owner.id, kind="trial_reminder", message=f"Your trial has {days_left} day(s) remaining."))
                                db.commit()
                    if kill_switch_enabled() or _active_daily_job(db, workspace.id) or _remaining_daily_slots(db, workspace.id) <= 0:
                        continue
                    date_key = f"{workspace.id}:{local.date().isoformat()}"
                    initial_key = f"tailormail:scheduled:{date_key}"
                    if client.set(initial_key, "1", nx=True, ex=172800):
                        enqueue_job("daily_outreach", {"scheduled_at": local.isoformat()}, workspace.id, workspace.owner_user_id)
                        continue
                    # A partial run is not the end of the day. Retry only after
                    # a configured interval, which gives providers time to show
                    # new roles while avoiding a tight loop against their APIs.
                    if local < scheduled_at + timedelta(minutes=settings.outreach_retry_minutes):
                        continue
                    retry_key = f"tailormail:daily-retry:{date_key}"
                    if client.set(retry_key, "1", nx=True, ex=settings.outreach_retry_minutes * 60):
                        enqueue_job("daily_outreach", {"retry_at": local.isoformat()}, workspace.id, workspace.owner_user_id)
        except Exception:
            # Keep the scheduler alive during a transient database restart/migration.
            time.sleep(5)
        time.sleep(30)


if __name__ == "__main__":
    run()
