"""Timezone-aware workspace scheduler. Enqueues at most one job per workspace/day."""
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import redis
from sqlalchemy import select

from .config import settings
from .db import SessionLocal
from .models import Notification, User, Subscription, Workspace


def run() -> None:
    client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    while True:
        try:
            with SessionLocal() as db:
                workspaces = db.scalars(select(Workspace).where(Workspace.deleted_at.is_(None), Workspace.paused.is_(False))).all()
                for workspace in workspaces:
                    try:
                        local = datetime.now(ZoneInfo(workspace.timezone))
                    except Exception:
                        continue
                    if local.strftime("%H:%M") != workspace.schedule_time:
                        continue
                    subscription = db.scalar(select(Subscription).where(Subscription.workspace_id == workspace.id).order_by(Subscription.id.desc()))
                    if not subscription or subscription.status not in {"active", "trialing", "past_due"}:
                        continue
                    if subscription.status == "trialing" and subscription.trial_ends_at:
                        days_left = (subscription.trial_ends_at.date() - local.date()).days
                        if days_left in {5, 2, 0}:
                            reminder_key = f"tailormail:trial-reminder:{workspace.id}:{days_left}:{local.date().isoformat()}"
                            owner = db.get(User, workspace.owner_user_id)
                            if owner and client.set(reminder_key, "1", nx=True, ex=172800):
                                client.lpush("tailormail:jobs", json.dumps({"type": "trial_reminder", "workspace_id": workspace.id, "email": owner.email, "days_left": days_left}))
                                db.add(Notification(workspace_id=workspace.id, user_id=owner.id, kind="trial_reminder", message=f"Your trial has {days_left} day(s) remaining."))
                                db.commit()
                    key = f"tailormail:scheduled:{workspace.id}:{local.date().isoformat()}"
                    if client.set(key, "1", nx=True, ex=172800):
                        client.lpush("tailormail:jobs", json.dumps({"type": "daily_outreach", "workspace_id": workspace.id, "scheduled_at": local.isoformat()}))
        except Exception:
            # Keep the scheduler alive during a transient database restart/migration.
            time.sleep(5)
        time.sleep(30)


if __name__ == "__main__":
    run()
