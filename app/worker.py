"""Small Redis-backed job runner for long outreach operations."""
import json
import logging
import redis

from .config import settings

log = logging.getLogger("tailormail.worker")


def run() -> None:
    client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    while True:
        _, payload = client.brpop("tailormail:jobs")
        lock_key = None
        try:
            job = json.loads(payload)
            lock_key = f"tailormail:running:{job.get('type')}:{job.get('workspace_id', 'global')}"
            if not client.set(lock_key, "1", nx=True, ex=7200):
                continue
            if job.get("type") == "daily_outreach":
                from .daily_outreach import main
                main(job.get("workspace_id"))
            elif job.get("type") == "trial_reminder":
                from .services import send_system_email
                days_left = job.get("days_left", 0)
                send_system_email(job["email"], "Your TailorMail trial is ending", f"Your TailorMail trial has {days_left} day(s) remaining. Review billing at {settings.base_url}/billing.")
            else:
                log.warning("Unknown job type: %s", job.get("type"))
        except Exception:
            log.exception("Outreach job failed")
            try:
                attempts = int(job.get("attempts", 0)) + 1
                if attempts < 5:
                    job["attempts"] = attempts
                    client.lpush("tailormail:jobs", json.dumps(job))
            except Exception:
                log.exception("Could not requeue failed job")
        finally:
            if lock_key:
                client.delete(lock_key)


if __name__ == "__main__":
    run()
