"""Durable operational state shared by web, worker, and scheduler processes."""
import json
from datetime import datetime, timezone

import redis
from sqlalchemy import select

from .config import settings
from .db import SessionLocal
from .models import OutreachJob, SystemSetting, utc_now


def setting_value(db, key: str, default: str = "") -> str:
    item = db.scalar(select(SystemSetting).where(SystemSetting.key == key))
    return item.value if item else default


def set_setting(db, key: str, value: str) -> None:
    item = db.scalar(select(SystemSetting).where(SystemSetting.key == key))
    if not item:
        item = SystemSetting(key=key, value=value)
        db.add(item)
    else:
        item.value = value


def kill_switch_enabled() -> bool:
    if settings.outreach_kill_switch:
        return True
    try:
        with SessionLocal() as db:
            return setting_value(db, "outreach_kill_switch", "false").lower() == "true"
    except Exception:
        return settings.outreach_kill_switch


def enqueue_job(job_type: str, payload: dict, workspace_id: int | None = None, user_id: int | None = None) -> int:
    with SessionLocal() as db:
        job = OutreachJob(workspace_id=workspace_id, user_id=user_id, job_type=job_type, payload=payload)
        db.add(job)
        db.commit()
        db.refresh(job)
        payload = {**payload, "job_id": job.id, "type": job_type}
    redis.Redis.from_url(settings.redis_url).lpush("tailormail:jobs", json.dumps(payload))
    return job.id


def mark_job(job_id: int, status: str, *, progress: int | None = None, error: str | None = None, attempts: int | None = None) -> None:
    with SessionLocal() as db:
        job = db.get(OutreachJob, job_id)
        if not job:
            return
        job.status = status
        if progress is not None:
            job.progress = max(0, min(progress, 100))
        if error is not None:
            job.error = error[:2000]
        if attempts is not None:
            job.attempts = attempts
        if status == "running" and not job.started_at:
            job.started_at = utc_now()
        if status in {"succeeded", "partial", "failed", "cancelled"}:
            job.finished_at = utc_now()
        db.commit()
