"""Single entitlement boundary for plan, trial, and send limits."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import func, select

from .config import settings
from .models import Candidate, Campaign, Plan, Subscription, Workspace


def _now():
    return datetime.now(timezone.utc)


def subscription_for(db, workspace: Workspace):
    return db.scalar(select(Subscription).where(Subscription.workspace_id == workspace.id).order_by(Subscription.id.desc()))


def plan_for(db, workspace: Workspace):
    subscription = subscription_for(db, workspace)
    return (subscription, db.get(Plan, subscription.plan_id)) if subscription and subscription.plan_id else (subscription, None)


def status_for(db, workspace: Workspace) -> dict:
    subscription, plan = plan_for(db, workspace)
    now = _now()
    trial_active = bool(subscription and subscription.status == "trialing" and subscription.trial_ends_at and subscription.trial_ends_at > now)
    active = bool(subscription and subscription.status in {"active", "trialing", "past_due"} and (trial_active or subscription.status != "trialing"))
    limits = (plan.limits if plan and isinstance(plan.limits, dict) else {})
    return {"subscription": subscription, "plan": plan, "trial_active": trial_active, "active": active, "limits": limits, "trial_days_left": max((subscription.trial_ends_at - now).days, 0) if trial_active else 0}


def require_feature(db, workspace: Workspace, feature: str) -> dict:
    status = status_for(db, workspace)
    if not status["active"]:
        raise HTTPException(402, "Your trial or subscription is inactive. Upgrade to continue.")
    if status["limits"].get(feature) is False:
        raise HTTPException(402, "This feature is not included in your plan.")
    return status


def require_candidate_slot(db, workspace: Workspace, additional: int = 1) -> None:
    status = require_feature(db, workspace, "cv_profiles")
    limit = int(status["limits"].get("cv_profiles", 1))
    count = db.scalar(select(func.count(Candidate.id)).where(Candidate.workspace_id == workspace.id, Candidate.deleted_at.is_(None))) or 0
    if count + additional > limit:
        raise HTTPException(402, f"Your plan allows {limit} CV profile(s). Upgrade to add another.")


def require_send_slot(db, workspace: Workspace) -> None:
    status = require_feature(db, workspace, "daily_emails")
    limit = int(status["limits"].get("daily_emails", settings.outreach_daily_limit))
    local = datetime.now(ZoneInfo(workspace.timezone))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    count = db.scalar(select(func.count(Campaign.id)).where(Campaign.workspace_id == workspace.id, Campaign.status == "sent", Campaign.sent_at >= start)) or 0
    if count >= limit:
        raise HTTPException(429, f"Daily email limit reached ({limit}).")
