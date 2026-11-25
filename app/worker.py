"""Small Redis-backed job runner for long outreach operations."""
import json
import logging
import time
import redis

from .config import settings
from .ops import mark_job
from .db import SessionLocal
from .models import OutreachJob

log = logging.getLogger("tailormail.worker")


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
                mark_job(int(job_id), "running", progress=1, attempts=int(job.get("attempts", 0)))
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
            log.exception("Outreach job failed")
            if job.get("job_id"):
                mark_job(int(job["job_id"]), "failed", progress=100, error=str(exc)[:2000] or "Job failed; see worker logs.", attempts=int(job.get("attempts", 0)) + 1)
            try:
                attempts = int(job.get("attempts", 0)) + 1
                if attempts < 5:
                    job["attempts"] = attempts
                    client.lpush("tailormail:jobs", json.dumps(job))
            except Exception:
                log.exception("Could not requeue failed job")
        finally:
            if lock_key and lock_acquired:
                client.delete(lock_key)


if __name__ == "__main__":
    run()
