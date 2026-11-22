from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import re
import secrets
import hashlib
import hmac
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import settings, update_settings
from .db import Base, SessionLocal, engine
from .entitlements import require_candidate_slot, require_send_slot, status_for
from .models import AIProviderConfig, AuditEvent, BillingEvent, Campaign, Candidate, EmailAccount, Plan, Subscription, Suppression, User, Workspace, WorkspaceMembership
from .security import encrypt_secret, hash_password, utc_now, verify_password
from .tenancy import backfill_legacy_rows, current_workspace, ensure_default_plans, ensure_default_workspace, require_owned, workspace_filter
from .services import (
    candidates_from_zip,
    cv_outreach_readiness_error,
    discover_company_lead,
    discover_company_leads,
    render_pdf,
    research_company,
    send_email,
    tailor,
    test_llm_connection,
    test_smtp_connection,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Path("data").mkdir(exist_ok=True)
    settings.artifact_dir.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        backfill_legacy_rows(db)
    yield


app = FastAPI(title="TailorMail", lifespan=lifespan)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=[host.strip() for host in settings.allowed_hosts.split(",") if host.strip()],
)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if (
            settings.admin_username
            and request.url.path not in {"/healthz", "/login", "/signup", "/landing", "/pricing", "/terms", "/privacy", "/acceptable-use", "/refunds", "/contact", "/sitemap.xml", "/robots.txt", "/unsubscribe"}
            and not request.url.path.startswith("/static/")
            and not request.session.get("authenticated")
        ):
            target = request.url.path
            if request.url.query:
                target = f"{target}?{request.url.query}"
            return RedirectResponse(f"/login?next={quote(target, safe='/?=&')}", status_code=303)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com data:; "
            "img-src 'self' data:; "
            "frame-src 'self'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        return response


app.add_middleware(SecurityMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.app_secret,
    session_cookie="tailormail_session",
    max_age=60 * 60 * 12,
    same_site="lax",
    https_only=settings.environment == "production",
)


@app.get("/healthz", include_in_schema=False)
def healthz():
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Database unavailable") from exc
    return {"status": "ok"}


def _safe_next(next_path: str) -> str:
    return next_path if next_path.startswith("/") and not next_path.startswith("//") else "/"


@app.get("/login")
def login_page(request: Request, next: str = "/"):
    if request.session.get("authenticated"):
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(request, "login.html", {"next": _safe_next(next), "error": ""})


@app.get("/signup")
def signup_page(request: Request):
    return templates.TemplateResponse(request, "signup.html", {"error": ""})


@app.get("/landing")
def landing_page(request: Request):
    return templates.TemplateResponse(request, "landing.html", {})


@app.get("/pricing")
def pricing_page(request: Request):
    with SessionLocal() as db:
        plans = db.scalars(select(Plan).where(Plan.active.is_(True)).order_by(Plan.id)).all()
        if not plans:
            plans = [
                Plan(slug="starter", name="Starter", limits={"daily_emails": 25, "cv_profiles": 1}),
                Plan(slug="pro", name="Pro", limits={"daily_emails": 100, "cv_profiles": 5}),
                Plan(slug="agency", name="Agency", limits={"daily_emails": 500, "cv_profiles": 25}),
            ]
        return templates.TemplateResponse(request, "pricing.html", {"plans": plans})


@app.get("/{page_name}")
def public_information_page(request: Request, page_name: str):
    pages = {
        "terms": ("Terms of service", "Use TailorMail responsibly and keep your account credentials secure."),
        "privacy": ("Privacy policy", "Workspace data is isolated and used to operate the features you configure."),
        "acceptable-use": ("Acceptable use", "Do not use TailorMail for spam, unlawful profiling, harassment, or deceptive outreach."),
        "refunds": ("Refund policy", "Subscription refunds are handled according to the plan and billing terms shown at checkout."),
        "contact": ("Contact", "Contact support through your workspace support channel."),
    }
    if page_name not in pages:
        raise HTTPException(404, "Page not found")
    title, body = pages[page_name]
    return templates.TemplateResponse(request, "legal.html", {"title": title, "body": body})


@app.get("/robots.txt", include_in_schema=False)
def robots():
    return PlainTextResponse("User-agent: *\nAllow: /landing\nAllow: /pricing\n")


@app.get("/sitemap.xml", include_in_schema=False)
def sitemap(request: Request):
    base = str(request.base_url).rstrip("/")
    return PlainTextResponse("\n".join(f"{base}/{path}" for path in ("landing", "pricing", "terms", "privacy", "acceptable-use", "refunds", "contact")), media_type="application/xml")


@app.post("/signup")
def signup(
    request: Request,
    email: str = Form(),
    password: str = Form(),
    terms: bool = Form(False),
):
    if not terms:
        return templates.TemplateResponse(request, "signup.html", {"error": "You must accept the acceptable-use and privacy terms."}, status_code=400)
    email = email.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return templates.TemplateResponse(request, "signup.html", {"error": "Enter a valid email address."}, status_code=400)
    try:
        password_hash = hash_password(password)
    except ValueError as exc:
        return templates.TemplateResponse(request, "signup.html", {"error": str(exc)}, status_code=400)
    with SessionLocal() as db:
        if db.scalar(select(User).where(User.email == email)):
            return templates.TemplateResponse(request, "signup.html", {"error": "An account with that email already exists."}, status_code=409)
        user = User(email=email, password_hash=password_hash, role="user", terms_accepted_at=utc_now())
        db.add(user)
        db.flush()
        slug = re.sub(r"[^a-z0-9]+", "-", email.split("@", 1)[0]).strip("-") or "workspace"
        if db.scalar(select(Workspace).where(Workspace.slug == slug)):
            slug = f"{slug}-{user.id}"
        workspace = Workspace(name=f"{email}'s workspace", slug=slug, owner_user_id=user.id)
        db.add(workspace)
        db.flush()
        db.add(WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="owner"))
        ensure_default_plans(db)
        starter = db.scalar(select(Plan).where(Plan.slug == "starter"))
        db.add(Subscription(workspace_id=workspace.id, plan_id=starter.id, status="trialing", trial_ends_at=utc_now() + timedelta(days=15)))
        db.commit()
        request.session.clear()
        request.session.update({"authenticated": True, "user_id": user.id, "workspace_id": workspace.id})
    return RedirectResponse("/", status_code=303)


@app.post("/login")
def login(request: Request, username: str = Form(), password: str = Form(), next: str = Form("/")):
    email = username.strip().lower()
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email))
        if user and user.password_hash and user.is_active and not user.locked_until and verify_password(user.password_hash, password):
            user.failed_login_count = 0
            user.last_login_at = utc_now()
            membership = db.scalar(select(WorkspaceMembership).where(WorkspaceMembership.user_id == user.id).order_by(WorkspaceMembership.created_at))
            if membership:
                db.commit()
                request.session.clear()
                request.session.update({"authenticated": True, "user_id": user.id, "workspace_id": membership.workspace_id})
                return RedirectResponse(_safe_next(next), status_code=303)
        if user and user.password_hash and user.is_active:
            user.failed_login_count += 1
            if user.failed_login_count >= 5:
                user.locked_until = utc_now() + timedelta(minutes=15)
            db.commit()
    if settings.admin_username and secrets.compare_digest(username, settings.admin_username) and secrets.compare_digest(password, settings.admin_password):
        with SessionLocal() as db:
            workspace, user = ensure_default_workspace(db)
            db.commit()
        request.session.clear()
        request.session["authenticated"] = True
        request.session["workspace_id"] = workspace.id
        request.session["user_id"] = user.id
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"next": _safe_next(next), "error": "Incorrect username or password."},
        status_code=401,
    )


@app.get("/account/export")
def export_account(request: Request):
    with SessionLocal() as db:
        workspace, user = current_workspace(request, db)
        candidates = db.scalars(select(Candidate).where(workspace_filter(Candidate, workspace))).all()
        campaigns = db.scalars(select(Campaign).where(workspace_filter(Campaign, workspace))).all()
    return JSONResponse({"user": {"id": user.id, "email": user.email}, "workspace": {"id": workspace.id, "name": workspace.name}, "candidates": [{"id": item.id, "name": item.name, "email": item.email, "cv_yaml": item.cv_yaml} for item in candidates], "campaigns": [{"id": item.id, "company_name": item.company_name, "recipient_email": item.recipient_email, "status": item.status, "email_subject": item.email_subject, "email_body": item.email_body} for item in campaigns]})


@app.post("/billing/webhook", include_in_schema=False)
async def billing_webhook(request: Request):
    if not settings.stripe_webhook_secret:
        raise HTTPException(503, "Stripe webhooks are not configured.")
    body = await request.body()
    signature = request.headers.get("stripe-signature", "")
    parts = {part.split("=", 1)[0]: part.split("=", 1)[1] for part in signature.split(",") if "=" in part}
    timestamp = parts.get("t")
    provided = parts.get("v1")
    if not timestamp or not provided:
        raise HTTPException(400, "Invalid Stripe signature.")
    signed = f"{timestamp}.{body.decode('utf-8')}".encode("utf-8")
    expected = hmac.new(settings.stripe_webhook_secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, provided):
        raise HTTPException(400, "Invalid Stripe signature.")
    try:
        event = json.loads(body)
        event_id = event["id"]
        event_type = event["type"]
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(400, "Invalid Stripe event.") from exc
    with SessionLocal() as db:
        if db.scalar(select(BillingEvent).where(BillingEvent.provider_event_id == event_id)):
            return {"ok": True, "duplicate": True}
        db.add(BillingEvent(provider_event_id=event_id, event_type=event_type, payload=event))
        data = event.get("data", {}).get("object", {})
        workspace_id = data.get("metadata", {}).get("workspace_id")
        subscription = db.scalar(select(Subscription).where(Subscription.workspace_id == int(workspace_id))) if workspace_id else None
        if subscription:
            if event_type in {"customer.subscription.created", "customer.subscription.updated"}:
                subscription.status = data.get("status", subscription.status)
                subscription.provider_customer_id = data.get("customer") or subscription.provider_customer_id
                subscription.provider_subscription_id = data.get("id") or subscription.provider_subscription_id
            elif event_type == "customer.subscription.deleted":
                subscription.status = "canceled"
            elif event_type == "invoice.payment_failed":
                subscription.status = "past_due"
                subscription.grace_ends_at = utc_now() + timedelta(days=7)
            elif event_type == "invoice.paid":
                subscription.status = "active"
        db.commit()
    return {"ok": True}


@app.post("/account/delete")
def delete_account(request: Request):
    with SessionLocal() as db:
        workspace, user = current_workspace(request, db)
        workspace.deleted_at = utc_now()
        user.is_active = False
        db.execute(update(Candidate).where(workspace_filter(Candidate, workspace)).values(deleted_at=utc_now()))
        db.execute(update(Campaign).where(workspace_filter(Campaign, workspace)).values(deleted_at=utc_now()))
        db.commit()
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/unsubscribe")
def unsubscribe(email: str, workspace_id: int | None = None):
    normalized = email.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", normalized):
        raise HTTPException(400, "A valid email address is required.")
    with SessionLocal() as db:
        db.add(Suppression(workspace_id=workspace_id, email=normalized, reason="unsubscribe"))
        db.commit()
    return PlainTextResponse("You have been unsubscribed.")


def audit(db, campaign: Campaign, action: str, detail: str, user_id: int | None = None) -> None:
    db.add(AuditEvent(campaign_id=campaign.id, workspace_id=campaign.workspace_id, user_id=user_id or campaign.user_id, action=action, detail=detail))


def remaining_daily_send_budget(db, workspace_id: int | None = None) -> int:
    try:
        local_timezone = ZoneInfo(settings.outreach_timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("OUTREACH_TIMEZONE must be a valid IANA timezone.") from exc
    now = datetime.now(local_timezone)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    filters = [Campaign.status == "sent", Campaign.sent_at >= start]
    if workspace_id is not None:
        filters.append(Campaign.workspace_id == workspace_id)
    sent_today = db.scalar(select(func.count(Campaign.id)).where(*filters)) or 0
    return max(settings.outreach_daily_limit - sent_today, 0)


@app.get("/")
def dashboard(request: Request, tab: str = "applications"):
    with SessionLocal() as db:
        workspace, _ = current_workspace(request, db)
        campaigns = db.scalars(select(Campaign).where(workspace_filter(Campaign, workspace), Campaign.deleted_at.is_(None)).order_by(Campaign.created_at.desc())).all()
        candidates = db.scalars(select(Candidate).where(workspace_filter(Candidate, workspace), Candidate.deleted_at.is_(None)).order_by(Candidate.name)).all()
        counts = dict(db.execute(select(Campaign.status, func.count(Campaign.id)).where(workspace_filter(Campaign, workspace), Campaign.deleted_at.is_(None)).group_by(Campaign.status)).all())
        try:
            daily_remaining = remaining_daily_send_budget(db, workspace.id)
        except ValueError:
            daily_remaining = 0
        subscription_status = status_for(db, workspace)
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "campaigns": campaigns,
                "candidates": candidates,
                "counts": counts,
                "daily_sent": settings.outreach_daily_limit - daily_remaining,
                "daily_remaining": daily_remaining,
                "active_tab": tab,
                "settings": settings,
                "subscription_status": subscription_status,
            },
        )


@app.post("/candidates")
def create_candidate(request: Request = None, name: str = Form(), email: str = Form(), cv_yaml: str = Form(), consent: bool = Form(False)):
    if not consent:
        raise HTTPException(400, "Candidate consent is required before processing their CV.")
    with SessionLocal() as db:
        workspace, user = current_workspace(request, db) if request else ensure_default_workspace(db)
        require_candidate_slot(db, workspace)
        candidate = Candidate(workspace_id=workspace.id, user_id=user.id, name=name.strip(), email=email.strip(), cv_yaml=cv_yaml)
        db.add(candidate)
        db.commit()
    return RedirectResponse("/?tab=candidates", status_code=303)


@app.post("/candidates/import")
async def import_candidates(request: Request = None, archive: UploadFile = File(), consent: bool = Form(False)):
    if not consent:
        raise HTTPException(400, "Candidate consent is required before processing CVs.")
    try:
        records = candidates_from_zip(await archive.read())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    with SessionLocal() as db:
        workspace, user = current_workspace(request, db) if request else ensure_default_workspace(db)
        require_candidate_slot(db, workspace, len(records))
        db.add_all([Candidate(workspace_id=workspace.id, user_id=user.id, name=name, email=email, cv_yaml=cv_yaml) for name, email, cv_yaml in records])
        db.commit()
    return RedirectResponse("/?tab=candidates", status_code=303)


@app.get("/candidates/{candidate_id}")
def get_candidate(request: Request, candidate_id: int):
    with SessionLocal() as db:
        workspace, _ = current_workspace(request, db)
        candidate = require_owned(db, Candidate, candidate_id, workspace)
        return {
            "id": candidate.id,
            "name": candidate.name,
            "email": candidate.email,
            "cv_yaml": candidate.cv_yaml,
            "campaigns_count": len(candidate.campaigns),
        }


@app.post("/candidates/{candidate_id}/delete")
def delete_candidate(request: Request, candidate_id: int):
    with SessionLocal() as db:
        workspace, _ = current_workspace(request, db)
        candidate = require_owned(db, Candidate, candidate_id, workspace)
        candidate.deleted_at = datetime.now(timezone.utc)
        db.commit()
    return RedirectResponse("/?tab=candidates", status_code=303)


@app.post("/campaigns")
def create_campaign(
    request: Request = None,
    candidate_id: int = Form(),
    company_name: str = Form(),
    company_url: str = Form(""),
    target_role: str = Form(""),
    job_description: str = Form(""),
    recipient_email: str = Form(),
    auto_prepare: bool = Form(False),
):
    with SessionLocal() as db:
        workspace, user = current_workspace(request, db) if request else ensure_default_workspace(db)
        candidate = require_owned(db, Candidate, candidate_id, workspace)
        campaign = Campaign(
            workspace_id=workspace.id,
            user_id=user.id,
            candidate_id=candidate_id,
            company_name=company_name.strip(),
            company_url=company_url.strip() or None,
            target_role=target_role.strip() or None,
            job_description=job_description.strip() or None,
            recipient_email=recipient_email.strip(),
        )
        db.add(campaign)
        db.commit()
        db.refresh(campaign)
        audit(db, campaign, "created", "Campaign created. Nothing sent.")
        db.commit()
        campaign_id = campaign.id

    if auto_prepare:
        return prepare_campaign(campaign_id, request=request)

    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@app.post("/leads/discover")
def discover_lead(request: Request = None, candidate_id: int = Form(), query: str = Form(), target_role: str = Form("")):
    with SessionLocal() as db:
        workspace, _ = current_workspace(request, db) if request else ensure_default_workspace(db)
        require_owned(db, Candidate, candidate_id, workspace)
    try:
        lead = discover_company_lead(query.strip())
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return create_campaign(
        candidate_id=candidate_id,
        company_name=lead["company_name"],
        company_url=lead["company_url"],
        target_role=target_role,
        job_description=f"Companies House number: {lead['company_number']}. Public contact sources: {', '.join(lead['source_urls'])}",
        recipient_email=lead["recipient_email"],
        auto_prepare=True,
        request=request,
    )


@app.post("/leads/batch")
def discover_lead_batch(
    request: Request = None,
    candidate_id: int = Form(),
    query: str = Form(),
    target_role: str = Form(""),
    limit: int = Form(100),
    send_when_ready: bool = Form(False),
):
    limit = min(max(limit, 1), 100)
    with SessionLocal() as db:
        workspace, _ = current_workspace(request, db) if request else ensure_default_workspace(db)
        require_owned(db, Candidate, candidate_id, workspace)
        existing_names = {name.casefold() for name in db.scalars(select(Campaign.company_name).where(workspace_filter(Campaign, workspace), Campaign.deleted_at.is_(None))).all()}
        existing_numbers = {
            number for description in db.scalars(select(Campaign.job_description).where(workspace_filter(Campaign, workspace), Campaign.deleted_at.is_(None))).all()
            for number in re.findall(r"Companies House number: ([A-Z0-9]+)", description or "")
        }
        if send_when_ready:
            try:
                limit = min(limit, remaining_daily_send_budget(db, workspace.id))
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
    if limit == 0:
        return JSONResponse({"discovered": 0, "completed": 0, "sent": 0, "message": "Daily send limit reached."})
    try:
        leads = discover_company_leads(query.strip(), limit, existing_names, existing_numbers)
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc

    completed = 0
    for lead in leads:
        try:
            response = create_campaign(
                candidate_id=candidate_id,
                company_name=lead["company_name"],
                company_url=lead["company_url"],
                target_role=target_role,
                job_description=f"Companies House number: {lead['company_number']}. Public contact sources: {', '.join(lead['source_urls'])}",
                recipient_email=lead["recipient_email"],
                auto_prepare=True,
                request=request,
            )
            if send_when_ready:
                campaign_id = int(response.headers["location"].rsplit("/", 1)[-1])
                send_campaign(campaign_id, approved=True, request=request)
            completed += 1
        except HTTPException:
            continue
    return JSONResponse({"discovered": len(leads), "completed": completed, "sent": completed if send_when_ready else 0})


@app.get("/campaigns/{campaign_id}")
def campaign_detail(request: Request, campaign_id: int):
    with SessionLocal() as db:
        workspace, _ = current_workspace(request, db)
        campaign = require_owned(db, Campaign, campaign_id, workspace)
        return templates.TemplateResponse(
            request,
            "campaign.html",
            {
                "campaign": campaign,
                "events": campaign.events,
                "cv_readiness_error": cv_outreach_readiness_error(campaign.candidate.cv_yaml),
                "settings": settings,
            },
        )


@app.post("/campaigns/{campaign_id}/prepare")
def prepare_campaign(campaign_id: int, request: Request = None):
    with SessionLocal() as db:
        workspace, _ = current_workspace(request, db) if request else ensure_default_workspace(db)
        campaign = require_owned(db, Campaign, campaign_id, workspace)
        try:
            campaign.status = "researching"
            db.commit()

            campaign.research = research_company(
                campaign.company_name,
                campaign.company_url,
                target_role=campaign.target_role,
                job_description=campaign.job_description,
            )

            result = tailor(
                campaign.candidate.cv_yaml,
                campaign.research,
                campaign.company_name,
                target_role=campaign.target_role,
                job_description=campaign.job_description,
                ai_config=db.scalar(select(AIProviderConfig).where(AIProviderConfig.workspace_id == workspace.id)),
            )

            campaign.tailored_yaml = result["cv_yaml"]
            campaign.email_subject = result["email_subject"]
            campaign.email_body = result["email_body"]
            campaign.artifact_path = str(render_pdf(campaign.tailored_yaml, campaign.id))
            campaign.status = "ready"
            audit(db, campaign, "prepared", "Research, tailoring, and RenderCV PDF render completed.")
            db.commit()
        except Exception as exc:
            campaign.status = "failed"
            audit(db, campaign, "failed", str(exc))
            db.commit()
            raise HTTPException(400, str(exc))
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@app.post("/campaigns/{campaign_id}/update-email")
def update_campaign_email(
    request: Request,
    campaign_id: int,
    email_subject: str = Form(),
    email_body: str = Form(),
):
    with SessionLocal() as db:
        workspace, user = current_workspace(request, db)
        campaign = require_owned(db, Campaign, campaign_id, workspace)
        if campaign.status not in {"ready", "failed"}:
            raise HTTPException(400, "Cannot edit email of sent or processing campaign")
        campaign.email_subject = email_subject.strip()
        campaign.email_body = email_body.strip()
        audit(db, campaign, "email_edited", "Application email subject and body updated by user.", user.id)
        db.commit()
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@app.get("/campaigns/{campaign_id}/cv.pdf")
def download_pdf(request: Request, campaign_id: int):
    with SessionLocal() as db:
        workspace, _ = current_workspace(request, db)
        campaign = require_owned(db, Campaign, campaign_id, workspace)
        if not campaign.artifact_path or not Path(campaign.artifact_path).is_file():
            raise HTTPException(404, "PDF not found")
        return FileResponse(
            campaign.artifact_path,
            media_type="application/pdf",
            filename=f"{campaign.candidate.name}-{campaign.company_name}-CV.pdf",
        )


@app.get("/campaigns/{campaign_id}/preview.pdf")
def preview_pdf(request: Request, campaign_id: int):
    with SessionLocal() as db:
        workspace, _ = current_workspace(request, db)
        campaign = require_owned(db, Campaign, campaign_id, workspace)
        if not campaign.artifact_path or not Path(campaign.artifact_path).is_file():
            raise HTTPException(404, "PDF not found")
        return FileResponse(
            campaign.artifact_path,
            media_type="application/pdf",
            headers={"Content-Disposition": "inline"},
        )


@app.post("/campaigns/{campaign_id}/send")
def send_campaign(campaign_id: int, approved: bool = Form(False), request: Request = None):
    if not approved:
        raise HTTPException(400, "Explicit approval is required to send.")
    with SessionLocal() as db:
        workspace, user = current_workspace(request, db)
        require_send_slot(db, workspace)
        recipient = campaign_recipient = db.scalar(select(Campaign.recipient_email).where(Campaign.id == campaign_id, Campaign.workspace_id == workspace.id))
        recipient_domain = recipient.rsplit("@", 1)[-1].lower() if recipient and "@" in recipient else ""
        suppressed = db.scalar(select(Suppression).where(Suppression.email == (recipient or "").lower())) or db.scalar(select(Suppression).where(Suppression.domain == recipient_domain, Suppression.workspace_id.is_(None)))
        if suppressed:
            raise HTTPException(400, "This recipient is suppressed and cannot be contacted.")
        claimed = db.execute(
            update(Campaign)
            .where(Campaign.id == campaign_id, Campaign.status == "ready", workspace_filter(Campaign, workspace), Campaign.deleted_at.is_(None))
            .values(status="sending")
        )
        if claimed.rowcount != 1:
            raise HTTPException(400, "Only a ready campaign can be sent.")
        db.commit()
        campaign = require_owned(db, Campaign, campaign_id, workspace)
        readiness_error = cv_outreach_readiness_error(campaign.candidate.cv_yaml)
        if readiness_error:
            campaign.status = "ready"
            audit(db, campaign, "send_blocked", readiness_error)
            db.commit()
            raise HTTPException(400, readiness_error)
        try:
            provider_message_id = send_email(
                campaign.recipient_email,
                campaign.email_subject,
                campaign.email_body.rstrip() + "\n\nIf you prefer not to receive application emails, reply with 'unsubscribe'.",
                Path(campaign.artifact_path),
                email_account=db.scalar(select(EmailAccount).where(EmailAccount.workspace_id == workspace.id, EmailAccount.status == "active").order_by(EmailAccount.id)),
            )
            campaign.status = "sent"
            campaign.sent_at = datetime.now(timezone.utc)
            detail = f"One email sent to {campaign.recipient_email}."
            if provider_message_id:
                detail += f" Provider message ID: {provider_message_id}."
            audit(db, campaign, "sent", detail, user.id)
            db.commit()
        except Exception as exc:
            campaign.status = "send_uncertain"
            audit(db, campaign, "send_uncertain", str(exc))
            db.commit()
            raise HTTPException(
                400,
                f"Email delivery error: {exc}. Kept unsent state locked to prevent duplicate email.",
            )
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@app.post("/campaigns/{campaign_id}/delete")
def delete_campaign(request: Request, campaign_id: int):
    with SessionLocal() as db:
        workspace, _ = current_workspace(request, db)
        campaign = require_owned(db, Campaign, campaign_id, workspace)
        campaign.deleted_at = datetime.now(timezone.utc)
        db.commit()
    return RedirectResponse("/?tab=applications", status_code=303)


@app.post("/settings")
def save_settings(
    request: Request,
    base_url: str = Form(""),
    api_key: str = Form(""),
    llm_model: str = Form(""),
    smtp_host: str = Form(""),
    smtp_port: int = Form(587),
    smtp_username: str = Form(""),
    smtp_password: str = Form(""),
    smtp_from: str = Form(""),
    smtp_use_tls: bool = Form(False),
    resend_api_key: str = Form(""),
    resend_from: str = Form(""),
    companies_house_api_key: str = Form(""),
    scrapollo_binary: str = Form("scrapollo"),
    scrapollo_cookie_file: str = Form(""),
    scrapollo_input_file: str = Form(""),
    outreach_daily_limit: int = Form(100),
    outreach_policy: str = Form("review_required"),
    outreach_candidate_id: int = Form(0),
    outreach_query: str = Form(""),
    outreach_target_role: str = Form(""),
    outreach_auto_send: bool = Form(False),
    outreach_timezone: str = Form("Asia/Dhaka"),
):
    with SessionLocal() as db:
        workspace, _ = current_workspace(request, db)
        if api_key.strip():
            config = db.scalar(select(AIProviderConfig).where(AIProviderConfig.workspace_id == workspace.id))
            if not config:
                config = AIProviderConfig(workspace_id=workspace.id, provider="custom", base_url=base_url.strip() or settings.base_url, encrypted_api_key=encrypt_secret(api_key.strip()), model_name=llm_model.strip() or settings.llm_model)
                db.add(config)
            else:
                config.base_url = base_url.strip() or config.base_url
                config.encrypted_api_key = encrypt_secret(api_key.strip())
                config.model_name = llm_model.strip() or config.model_name
            db.commit()
    update_settings(
        base_url=base_url.strip() or settings.base_url,
        api_key=api_key.strip() or settings.api_key,
        llm_model=llm_model.strip() or settings.llm_model,
        smtp_host=smtp_host.strip() or settings.smtp_host,
        smtp_port=smtp_port,
        smtp_username=smtp_username.strip() or settings.smtp_username,
        smtp_password=smtp_password.strip() or settings.smtp_password,
        smtp_from=smtp_from.strip() or settings.smtp_from,
        smtp_use_tls=smtp_use_tls,
        resend_api_key=resend_api_key.strip() or settings.resend_api_key,
        resend_from=resend_from.strip() or settings.resend_from,
        companies_house_api_key=companies_house_api_key.strip() or settings.companies_house_api_key,
        scrapollo_binary=scrapollo_binary.strip() or "scrapollo",
        scrapollo_cookie_file=scrapollo_cookie_file.strip() or settings.scrapollo_cookie_file,
        scrapollo_input_file=scrapollo_input_file.strip() or settings.scrapollo_input_file,
        outreach_daily_limit=min(max(outreach_daily_limit, 1), 100),
        outreach_policy=outreach_policy if outreach_policy == "review_required" else "review_required",
        outreach_candidate_id=max(outreach_candidate_id, 0),
        outreach_query=outreach_query.strip(),
        outreach_target_role=outreach_target_role.strip(),
        outreach_auto_send=outreach_auto_send,
        outreach_timezone=outreach_timezone.strip() or settings.outreach_timezone,
    )
    return RedirectResponse("/?tab=settings", status_code=303)


@app.post("/settings/test-llm")
def api_test_llm(
    base_url: str = Form("http://127.0.0.1:31415"),
    api_key: str = Form(""),
    llm_model: str = Form("local-model"),
):
    result = test_llm_connection(base_url.strip(), api_key.strip(), llm_model.strip())
    return JSONResponse(result)


@app.post("/settings/test-smtp")
def api_test_smtp(
    smtp_host: str = Form(""),
    smtp_port: int = Form(587),
    smtp_username: str = Form(""),
    smtp_password: str = Form(""),
    smtp_use_tls: bool = Form(False),
):
    result = test_smtp_connection(
        smtp_host.strip(),
        smtp_port,
        smtp_username.strip(),
        smtp_password.strip(),
        smtp_use_tls,
    )
    return JSONResponse(result)


@app.post("/settings/email-account")
def save_email_account(
    request: Request,
    email: str = Form(),
    host: str = Form(),
    port: int = Form(587),
    username: str = Form(""),
    password: str = Form(""),
    use_tls: bool = Form(True),
):
    if not settings.encryption_key:
        raise HTTPException(503, "ENCRYPTION_KEY is not configured.")
    with SessionLocal() as db:
        workspace, _ = current_workspace(request, db)
        account = db.scalar(select(EmailAccount).where(EmailAccount.workspace_id == workspace.id).order_by(EmailAccount.id))
        payload = json.dumps({"host": host.strip(), "port": port, "username": username.strip(), "password": password, "use_tls": use_tls})
        if not account:
            account = EmailAccount(workspace_id=workspace.id, provider="smtp", email=email.strip(), encrypted_credentials=encrypt_secret(payload))
            db.add(account)
        else:
            account.email = email.strip()
            account.provider = "smtp"
            account.encrypted_credentials = encrypt_secret(payload)
            account.status = "active"
        db.commit()
    return RedirectResponse("/?tab=settings", status_code=303)
