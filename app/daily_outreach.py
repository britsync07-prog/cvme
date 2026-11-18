"""Run one configured daily outreach batch; invoke this from the server scheduler."""

import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import settings
from .db import SessionLocal
from .main import discover_lead_batch, remaining_daily_send_budget
from .models import Candidate
from .services import cv_outreach_readiness_error


def production_preflight_errors() -> list[str]:
    errors: list[str] = []
    if not settings.outreach_candidate_id:
        errors.append("OUTREACH_CANDIDATE_ID is required.")
    if not settings.outreach_query:
        errors.append("OUTREACH_QUERY is required.")
    if settings.outreach_daily_limit != 100:
        errors.append("OUTREACH_DAILY_LIMIT must be 100 for the configured daily target.")
    if not settings.companies_house_api_key:
        errors.append("COMPANIES_HOUSE_API_KEY is required.")
    if not settings.api_key or not settings.base_url or not settings.llm_model:
        errors.append("BASE_URL, API_KEY, and LLM_MODEL are required.")
    if settings.outreach_auto_send and not (
        (settings.resend_api_key and settings.resend_from) or (settings.smtp_host and settings.smtp_from)
    ):
        errors.append("Configured automatic sending requires Resend or SMTP sender settings.")
    try:
        ZoneInfo(settings.outreach_timezone)
    except ZoneInfoNotFoundError:
        errors.append("OUTREACH_TIMEZONE must be a valid IANA timezone.")
    if settings.outreach_candidate_id:
        with SessionLocal() as db:
            candidate = db.get(Candidate, settings.outreach_candidate_id)
            if not candidate:
                errors.append("Configured outreach candidate was not found.")
            else:
                readiness_error = cv_outreach_readiness_error(candidate.cv_yaml)
                if readiness_error:
                    errors.append(readiness_error)
    return errors


def main() -> None:
    errors = production_preflight_errors()
    if errors:
        raise SystemExit("\n".join(errors))
    response = discover_lead_batch(
        candidate_id=settings.outreach_candidate_id,
        query=settings.outreach_query,
        target_role=settings.outreach_target_role,
        limit=settings.outreach_daily_limit,
        send_when_ready=settings.outreach_auto_send,
    )
    result = json.loads(response.body)
    print(json.dumps(result))
    if settings.outreach_auto_send:
        with SessionLocal() as db:
            remaining = remaining_daily_send_budget(db)
        if remaining:
            raise SystemExit(f"Daily send target missed: {remaining} delivery slots remain.")
    elif result["completed"] < settings.outreach_daily_limit:
        raise SystemExit(
            f"Daily preparation target missed: prepared {result['completed']} of {settings.outreach_daily_limit}."
        )


if __name__ == "__main__":
    main()
