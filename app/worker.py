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
        try:
            job = json.loads(payload)
            if job.get("type") == "daily_outreach":
                from .daily_outreach import main
                main(job.get("workspace_id"))
            else:
                log.warning("Unknown job type: %s", job.get("type"))
        except Exception:
            log.exception("Outreach job failed")


if __name__ == "__main__":
    run()
