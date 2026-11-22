"""Run one configured daily outreach batch; invoke this from the server scheduler."""

import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from sqlalchemy import select

from .config import settings
from .db import SessionLocal
from .main import discover_lead_batch, remaining_daily_send_budget
from .models import Candidate, EmailAccount, Workspace
from .services import cv_outreach_readiness_error
from .tenancy import ensure_default_workspace


def production_preflight_errors(workspace_id: int | None = None) -> list[str]:
    errors: list[str] = []
    configured_candidate = settings.outreach_candidate_id
    configured_query = settings.outreach_query
    configured_role = settings.outreach_target_role
    configured_limit = settings.outreach_daily_limit
    configured_timezone = settings.outreach_timezone
    with SessionLocal() as db:
        workspace = None
        if workspace_id:
            workspace = db.get(Workspace, workspace_id)
        elif settings.outreach_candidate_id:
            workspace, _ = ensure_default_workspace(db)
        if workspace:
            configured_candidate = workspace.outreach_candidate_id
            configured_query = workspace.outreach_query
            configured_role = workspace.outreach_target_role
            configured_limit = workspace.daily_limit
            configured_timezone = workspace.timezone
            if workspace.paused:
                errors.append("Workspace outreach is paused.")
        elif workspace_id:
            errors.append("Workspace not found.")
    if not configured_candidate:
        errors.append("OUTREACH_CANDIDATE_ID is required.")
    if not configured_query:
        errors.append("OUTREACH_QUERY is required.")
    if not 1 <= configured_limit <= 100:
        errors.append("Daily outreach limit must be between 1 and 100.")
    if not settings.companies_house_api_key:
        errors.append("COMPANIES_HOUSE_API_KEY is required.")
    if not settings.api_key or not settings.base_url or not settings.llm_model:
        errors.append("BASE_URL, API_KEY, and LLM_MODEL are required.")
    if settings.outreach_auto_send:
        with SessionLocal() as db:
            sender = db.scalar(select(EmailAccount).where(EmailAccount.workspace_id == (workspace_id or (ensure_default_workspace(db)[0].id)), EmailAccount.status == "active"))
        if not sender and not (settings.resend_api_key and settings.resend_from):
            errors.append("Configured automatic sending requires a connected workspace sender.")
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
                    Candidate.workspace_id == workspace.id,
                    Candidate.deleted_at.is_(None),
                )
            )
            if not candidate:
                errors.append("Configured outreach candidate was not found.")
            else:
                readiness_error = cv_outreach_readiness_error(candidate.cv_yaml)
                if readiness_error:
                    errors.append(readiness_error)
    return errors


def main(workspace_id: int | None = None) -> None:
    errors = production_preflight_errors(workspace_id)
    if errors:
        raise SystemExit("\n".join(errors))
    totals = {"discovered": 0, "completed": 0, "sent": 0}
    while True:
        with SessionLocal() as db:
            remaining = remaining_daily_send_budget(db, workspace_id)
        if remaining == 0:
            print(json.dumps(totals))
            return
        with SessionLocal() as db:
            workspace = db.get(Workspace, workspace_id) if workspace_id else ensure_default_workspace(db)[0]
            candidate_id = workspace.outreach_candidate_id if workspace_id else settings.outreach_candidate_id
            query = workspace.outreach_query if workspace_id else settings.outreach_query
            target_role = workspace.outreach_target_role if workspace_id else settings.outreach_target_role
            auto_send = settings.outreach_auto_send
        response = discover_lead_batch(
            candidate_id=candidate_id,
            query=query,
            target_role=target_role,
            limit=1 if auto_send else remaining,
            send_when_ready=auto_send,
            workspace_id=workspace_id,
        )
        result = json.loads(response.body)
        for key in totals:
            totals[key] += result[key]
        if not settings.outreach_auto_send:
            print(json.dumps(totals))
            if result["completed"] < remaining:
                raise SystemExit(
                    f"Daily preparation target missed: prepared {result['completed']} of {remaining}."
                )
            return
        if result["discovered"] == 0:
            print(json.dumps(totals))
            action = "send" if settings.outreach_auto_send else "preparation"
            raise SystemExit(f"Daily {action} target missed: {remaining} slots remain; no new verified leads were available.")
        if result["completed"] == 0:
            # A failed AI/render/send attempt must not prevent trying the next lead.
            continue


if __name__ == "__main__":
    main()
