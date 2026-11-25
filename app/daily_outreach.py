"""Run one scheduled job-led outreach batch."""

import asyncio
import json
import random
import re
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from sqlalchemy import select

from .config import settings
from .db import SessionLocal
from .jobs import JobQuery, aggregate_search, find_public_company_contacts
from .main import _persist_jobs, create_campaign, remaining_daily_send_budget, send_campaign
from .models import Campaign, Candidate, EmailAccount, User, Workspace
from .ops import kill_switch_enabled, mark_job
from .services import cv_outreach_readiness_error
from .tenancy import ensure_default_workspace


def production_preflight_errors(workspace_id: int | None = None) -> list[str]:
    """Validate the scheduled job-search outreach configuration without sending."""
    errors: list[str] = []
    configured_candidate = settings.outreach_candidate_id
    configured_query = settings.outreach_query
    configured_role = settings.outreach_target_role
    configured_limit = settings.outreach_daily_limit
    configured_timezone = settings.outreach_timezone
    workspace_auto_send = settings.outreach_auto_send
    with SessionLocal() as db:
        workspace = None
        if workspace_id:
            workspace = db.get(Workspace, workspace_id)
        elif settings.outreach_candidate_id:
            workspace, _ = ensure_default_workspace(db)
        if workspace:
            configured_candidate = workspace.outreach_candidate_id or configured_candidate
            configured_query = workspace.outreach_query or configured_query
            configured_role = workspace.outreach_target_role or configured_role
            configured_limit = workspace.daily_limit or configured_limit
            configured_timezone = workspace.timezone or configured_timezone
            if workspace_id:
                workspace_auto_send = bool(getattr(workspace, "outreach_auto_send", workspace_auto_send))
            if workspace.paused:
                errors.append("Workspace outreach is paused.")
        elif workspace_id:
            errors.append("Workspace not found.")
    if not configured_candidate:
        errors.append("OUTREACH_CANDIDATE_ID is required.")
    if not (configured_query or configured_role):
        errors.append("OUTREACH_QUERY or OUTREACH_TARGET_ROLE is required for job search.")
    if not 1 <= configured_limit <= 100:
        errors.append("Daily outreach limit must be between 1 and 100.")
    if not 1 <= settings.outreach_max_search_pages <= 25:
        errors.append("OUTREACH_MAX_SEARCH_PAGES must be between 1 and 25.")
    if not 25 <= settings.outreach_search_page_size <= 100:
        errors.append("OUTREACH_SEARCH_PAGE_SIZE must be between 25 and 100.")
    if not settings.api_key or not settings.base_url or not settings.llm_model:
        errors.append("BASE_URL, API_KEY, and LLM_MODEL are required.")
    if workspace_auto_send:
        with SessionLocal() as db:
            sender = db.scalar(select(EmailAccount).where(EmailAccount.workspace_id == (workspace_id or (ensure_default_workspace(db)[0].id)), EmailAccount.status == "active"))
        if not sender and not (settings.resend_api_key and settings.resend_from):
            errors.append("Configured automatic sending requires Resend or SMTP / a connected workspace sender.")
    try:
        ZoneInfo(configured_timezone)
    except ZoneInfoNotFoundError:
        errors.append("OUTREACH_TIMEZONE must be a valid IANA timezone.")
    if configured_candidate:
        with SessionLocal() as db:
            workspace = db.get(Workspace, workspace_id) if workspace_id else ensure_default_workspace(db)[0]
            if not workspace:
                errors.append("Workspace not found.")
                return errors
            candidate = db.scalar(
                select(Candidate).where(
                    Candidate.id == configured_candidate,
                    (Candidate.workspace_id == workspace.id) | Candidate.workspace_id.is_(None),
                    Candidate.deleted_at.is_(None),
                )
            )
            if not candidate:
                errors.append("Configured outreach candidate was not found.")
            else:
                if candidate.workspace_id is None:
                    candidate.workspace_id = workspace.id
                    db.commit()
                readiness_error = cv_outreach_readiness_error(candidate.cv_yaml)
                if readiness_error:
                    errors.append(readiness_error)
    return errors


def _search_variants(keywords: str) -> list[str]:
    """Treat configured semicolon/newline role phrases as alternatives, not one giant query."""
    variants: list[str] = []
    for value in re.split(r"[;|\n]+", keywords):
        value = value.strip()
        if value and value.casefold() not in {item.casefold() for item in variants}:
            variants.append(value)
    return variants or [keywords.strip()]


def discover_job_batch(
    *,
    workspace_id: int | None,
    candidate_id: int,
    keywords: str,
    target_role: str,
    limit: int,
    send_when_ready: bool,
) -> dict[str, int]:
    """Search pages and role variants until the daily send target is met or sources are exhausted."""
    limit = min(max(limit, 1), 100)
    totals = {"discovered": 0, "completed": 0, "sent": 0}
    processed_job_ids: set[int] = set()

    def persist_page(job_query: JobQuery, results, statuses) -> list[dict]:
        with SessionLocal() as db:
            workspace = db.get(Workspace, workspace_id) if workspace_id else ensure_default_workspace(db)[0]
            owner = db.get(User, workspace.owner_user_id) if workspace.owner_user_id else None
            if not owner:
                raise RuntimeError("Workspace owner was not found.")
            persisted_jobs = _persist_jobs(db, workspace, owner, job_query, results, statuses)
            # A commit expires ORM objects by default. Keep the immutable fields
            # needed for research after the database session closes.
            jobs = [
                {
                    "id": job.id,
                    "company_name": job.company_name,
                    "company_website": job.company_website,
                    "apply_url": job.apply_url,
                    "title": job.title,
                    "description": job.description,
                }
                for job in persisted_jobs
            ]
            db.commit()
        return jobs

    def process_jobs(jobs: list[dict]) -> None:
        for job in jobs:
            if totals["sent"] >= limit:
                return
            with SessionLocal() as db:
                existing = db.scalar(
                    select(Campaign.id).where(
                        Campaign.workspace_id == (workspace_id or ensure_default_workspace(db)[0].id),
                        Campaign.job_id == job["id"],
                        Campaign.deleted_at.is_(None),
                    )
                )
            if existing:
                continue

            try:
                contacts = find_public_company_contacts(
                    job["company_name"], job["company_website"], job["apply_url"], job["description"],
                )
            except (ValueError, OSError):
                continue
            contact = next((item for item in contacts if item.get("verified_mx")), None)
            if not contact:
                continue

            try:
                response = create_campaign(
                    workspace_id=workspace_id,
                    job_id=job["id"],
                    candidate_id=candidate_id,
                    company_name=job["company_name"],
                    company_url=job["company_website"] or job["apply_url"],
                    target_role=target_role or job["title"],
                    job_description=(job["description"] or f"Job listing: {job['apply_url']}") + f"\n\nPublic employer contact source: {contact['source_url']}\nVerified recipient: {contact['email']}",
                    recipient_email=contact["email"],
                    auto_prepare=True,
                )
                campaign_id = int(response.headers["location"].rsplit("/", 1)[-1])
                totals["completed"] += 1
                if send_when_ready:
                    send_campaign(campaign_id, approved=True, workspace_id=workspace_id)
                    totals["sent"] += 1
                    if settings.environment == "production":
                        time.sleep(random.uniform(max(settings.outreach_min_delay_seconds, 0), max(settings.outreach_max_delay_seconds, settings.outreach_min_delay_seconds)))
            except HTTPException:
                continue

    for search_term in _search_variants(keywords):
        for page in range(1, settings.outreach_max_search_pages + 1):
            if totals["sent"] >= limit:
                return totals
            job_query = JobQuery(keywords=search_term, page=page, page_size=settings.outreach_search_page_size)
            results, statuses = asyncio.run(aggregate_search(job_query))
            if not results:
                break
            jobs = persist_page(job_query, results, statuses)
            new_jobs = [job for job in jobs if job["id"] not in processed_job_ids]
            processed_job_ids.update(job["id"] for job in jobs)
            if not new_jobs:
                # Providers without pagination return the same first page; do
                # not burn the whole daily worker on repeated results. Still
                # try page two: page one may contain jobs saved by a prior run
                # while a paginated provider has fresh results behind it.
                if page > 1:
                    break
                continue
            totals["discovered"] += len(new_jobs)
            process_jobs(new_jobs)
    return totals


def main(workspace_id: int | None = None, job_id: int | None = None) -> None:
    if kill_switch_enabled():
        raise SystemExit("Outreach kill switch is enabled.")
    if job_id:
        mark_job(job_id, "running", progress=5)
    errors = production_preflight_errors(workspace_id)
    if errors:
        raise SystemExit("\n".join(errors))

    with SessionLocal() as db:
        remaining = remaining_daily_send_budget(db, workspace_id)
        workspace = db.get(Workspace, workspace_id) if workspace_id else ensure_default_workspace(db)[0]
        candidate_id = workspace.outreach_candidate_id or settings.outreach_candidate_id
        keywords = workspace.outreach_query or settings.outreach_query or workspace.outreach_target_role or settings.outreach_target_role
        target_role = workspace.outreach_target_role or settings.outreach_target_role
        auto_send = bool(getattr(workspace, "outreach_auto_send", settings.outreach_auto_send)) if workspace_id else settings.outreach_auto_send

    if remaining == 0:
        result = {"discovered": 0, "completed": 0, "sent": 0}
        status, error = "succeeded", None
    else:
        result = discover_job_batch(
            workspace_id=workspace_id,
            candidate_id=candidate_id,
            keywords=keywords,
            target_role=target_role,
            limit=remaining,
            send_when_ready=auto_send,
        )
        if auto_send and result["sent"] < remaining:
            status = "partial"
            error = f"Job-led outreach sent {result['sent']} of {remaining} available daily slots from {result['discovered']} job listings."
        elif not auto_send and result["completed"] < remaining:
            status = "partial"
            error = f"Job-led outreach prepared {result['completed']} campaigns from {result['discovered']} job listings."
        else:
            status, error = "succeeded", None

    print(json.dumps(result))
    if job_id:
        mark_job(job_id, status, progress=100, error=error)


if __name__ == "__main__":
    main()
