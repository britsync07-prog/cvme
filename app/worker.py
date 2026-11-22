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
            else:
                log.warning("Unknown job type: %s", job.get("type"))
        except Exception:
            log.exception("Outreach job failed")
        finally:
            if lock_key:
                client.delete(lock_key)


if __name__ == "__main__":
    run()
