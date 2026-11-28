"""Small Redis-backed job runner for long outreach operations."""
import json
import logging
import time
import redis
import httpx
from redis.exceptions import RedisError
from sqlalchemy.exc import DataError, IntegrityError, OperationalError, StatementError

from .config import settings
from .ops import mark_job
from .db import SessionLocal
from .models import OutreachJob

log = logging.getLogger("tailormail.worker")
MAX_RETRY_ATTEMPTS = 3


def classify_job_failure(exc: BaseException) -> tuple[bool, str]:
    """Return whether a queue job is safe to retry and a non-sensitive reason."""
    if isinstance(exc, SystemExit):
        return False, "preflight_failed"
    if isinstance(exc, OperationalError):
        return True, "database_unavailable"
    if isinstance(exc, (DataError, IntegrityError, StatementError, ValueError, TypeError, KeyError)):
        return False, "invalid_data_or_configuration"
    if isinstance(exc, (httpx.RequestError, RedisError, TimeoutError, ConnectionError)):
        return True, "external_service_unavailable"
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        if status_code == 429 or status_code >= 500:
            return True, f"http_{status_code}"
        return False, f"http_{status_code}"
    return False, "unexpected_failure"


def _attempt_count(job: dict) -> int:
    try:
        return max(int(job.get("attempts", 0)), 0)
    except (TypeError, ValueError):
        return 0


def run() -> None:
    client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    while True:
        _, payload = client.brpop("tailormail:jobs")
        lock_key = None
        lock_acquired = False
        job = {}
        try:
            job = json.loads(payload)
            job_id = job.get("job_id")
            if job_id:
                with SessionLocal() as db:
                    durable = db.get(OutreachJob, int(job_id))
                    if durable and durable.status == "cancelled":
                        continue
                mark_job(int(job_id), "running", progress=1, attempts=_attempt_count(job))
            lock_key = f"tailormail:running:{job.get('type')}:{job.get('workspace_id', 'global')}"
            if not client.set(lock_key, "1", nx=True, ex=7200):
                # Another worker owns this job type. Preserve the durable job
                # and queue payload; never leave it falsely marked running at
                # 1% or delete another worker's lock.
                if job_id:
                    mark_job(int(job_id), "queued", progress=0)
                client.lpush("tailormail:jobs", json.dumps(job))
                time.sleep(1)
                continue
            lock_acquired = True
            if job.get("type") == "daily_outreach":
                from .daily_outreach import main
                main(job.get("workspace_id"), job_id=job_id)
            elif job.get("type") == "prepare_campaign":
                from .main import prepare_campaign
                prepare_campaign(int(job["campaign_id"]), workspace_id=int(job["workspace_id"]))
                if job_id:
                    mark_job(int(job_id), "succeeded", progress=100)
            elif job.get("type") == "trial_reminder":
                from .services import send_system_email
                days_left = job.get("days_left", 0)
                send_system_email(job["email"], "Your TailorJobhunt trial is ending", f"Your TailorJobhunt trial has {days_left} day(s) remaining. Review billing at {settings.base_url}/billing.")
            else:
                log.warning("Unknown job type: %s", job.get("type"))
            if job_id and job.get("type") != "daily_outreach":
                mark_job(int(job_id), "succeeded", progress=100)
        except (Exception, SystemExit) as exc:
            retryable, reason = classify_job_failure(exc)
            attempts = _attempt_count(job) + 1
            log.warning(
                "outreach_job_failed type=%s retryable=%s reason=%s attempt=%s",
                job.get("type", "unknown"),
                retryable,
                reason,
                attempts,
            )
            should_retry = retryable and attempts < MAX_RETRY_ATTEMPTS
            if job.get("job_id"):
                mark_job(
                    int(job["job_id"]),
                    "queued" if should_retry else "failed",
                    progress=0 if should_retry else 100,
                    error=f"{reason} ({'retrying' if should_retry else 'not retried'})",
                    attempts=attempts,
                )
            if should_retry:
                try:
                    job["attempts"] = attempts
                    # The small bounded delay prevents immediate retry storms;
                    # circuit breaking handles longer provider outages separately.
                    time.sleep(min(2 ** attempts, 8))
                    client.lpush("tailormail:jobs", json.dumps(job))
                except Exception:
                    log.warning("outreach_job_requeue_failed type=%s", job.get("type", "unknown"))
        finally:
            if lock_key and lock_acquired:
                client.delete(lock_key)


if __name__ == "__main__":
    run()
