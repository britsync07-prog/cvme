from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import re
import secrets
import hashlib
import hmac
import redis
import pyotp
import logging
import sentry_sdk
from urllib.parse import quote, urlencode
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
from .entitlements import require_candidate_slot, require_feature, require_send_slot, status_for
from .models import AIProviderConfig, AuditEvent, AuditLog, BillingEvent, Campaign, Candidate, EmailAccount, EmailVerificationToken, Invoice, Notification, OutreachJob, PasswordResetToken, Plan, SessionToken, Subscription, SupportTicket, Suppression, UsageRecord, User, Workspace, WorkspaceMembership
from .ops import enqueue_job, kill_switch_enabled, mark_job, set_setting, setting_value
from .security import decrypt_secret, encrypt_secret, hash_password, new_token, token_hash, utc_now, verify_password
from .tenancy import backfill_legacy_rows, current_workspace, ensure_default_plans, ensure_default_workspace, require_owned, workspace_filter
from .services import (
    candidates_from_zip,
    cv_outreach_readiness_error,
    discover_company_lead,
    discover_company_leads,
    render_pdf,
    research_company,
    send_email,
    send_system_email,
    tailor,
    test_llm_connection,
    test_smtp_connection,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if settings.sentry_dsn:
        sentry_sdk.init(dsn=settings.sentry_dsn, traces_sample_rate=0.1)
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


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = new_token()
        request.session["csrf_token"] = token
    return token


templates.env.globals["csrf_token"] = csrf_token


def rate_limited(request: Request, bucket: str, limit: int, window: int = 900) -> bool:
    """Best-effort distributed limiter; availability of Redis must not take down auth."""
    try:
        address = request.client.host if request.client else "unknown"
        client = redis.Redis.from_url(settings.redis_url)
        key = f"tailormail:rate:{bucket}:{address}"
        count = client.incr(key)
        if count == 1:
            client.expire(key, window)
        return count > limit
    except Exception:
        return False


class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if (
            settings.admin_username
            and request.url.path not in {"/healthz", "/login", "/signup", "/forgot-password", "/reset-password", "/verify-email", "/auth/google", "/auth/google/callback", "/landing", "/pricing", "/terms", "/privacy", "/acceptable-use", "/refunds", "/contact", "/sitemap.xml", "/robots.txt", "/unsubscribe"}
            and not request.url.path.startswith("/static/")
            and not request.session.get("authenticated")
        ):
            target = request.url.path
            if request.url.query:
                target = f"{target}?{request.url.query}"
            return RedirectResponse(f"/login?next={quote(target, safe='/?=&')}", status_code=303)
        if request.session.get("authenticated") and request.url.path != "/verify-email" and not request.url.path.startswith("/static/"):
            with SessionLocal() as db:
                user = db.get(User, request.session.get("user_id")) if request.session.get("user_id") else None
                if user and user.role == "user" and not user.email_verified_at:
                    return RedirectResponse("/verify-email", status_code=303)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' https://challenges.cloudflare.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com data:; "
            "img-src 'self' data:; "
            "frame-src 'self' https://challenges.cloudflare.com; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        return response


class CSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path not in {"/healthz", "/billing/webhook", "/unsubscribe"}:
            if settings.environment != "production":
                return await call_next(request)
            body = await request.body()
            token = request.session.get("csrf_token")
            supplied = re.search(rb"(?:^|&)csrf_token=([^&]+)", body) or re.search(rb'name="csrf_token"\r?\n\r?\n([^\r\n]+)', body)
            if not token or not supplied or not secrets.compare_digest(supplied.group(1).decode("utf-8", "ignore"), token):
                return JSONResponse({"detail": "CSRF validation failed."}, status_code=403)
            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}
            request._receive = receive
        return await call_next(request)


app.add_middleware(SecurityMiddleware)
app.add_middleware(CSRFMiddleware)
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
        if settings.environment == "production":
            redis.Redis.from_url(settings.redis_url).ping()
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Database unavailable") from exc
    except redis.RedisError as exc:
        raise HTTPException(503, "Queue unavailable") from exc
    return {"status": "ok"}


def _safe_next(next_path: str) -> str:
    return next_path if next_path.startswith("/") and not next_path.startswith("//") else "/"


@app.get("/login")
def login_page(request: Request, next: str = "/", mfa: int = 0):
    if request.session.get("authenticated"):
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(request, "login.html", {"next": _safe_next(next), "error": "", "mfa": bool(mfa)})


@app.get("/signup")
def signup_page(request: Request):
    return templates.TemplateResponse(request, "signup.html", {"error": "", "settings": settings})


@app.get("/auth/google")
def google_login(request: Request):
    if not settings.google_client_id or not settings.google_redirect_uri:
        return templates.TemplateResponse(request, "auth_message.html", {"title": "Google sign-in unavailable", "message": "Google OAuth is not configured on this deployment."}, status_code=503)
    state = new_token()
    request.session["google_oauth_state"] = state
    params = urlencode({"client_id": settings.google_client_id, "redirect_uri": settings.google_redirect_uri, "response_type": "code", "scope": "openid email profile", "access_type": "offline", "prompt": "select_account", "state": state})
    return RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{params}", status_code=302)


@app.get("/auth/google/callback")
def google_callback(request: Request, code: str = "", state: str = ""):
    expected = request.session.pop("google_oauth_state", "")
    if not code or not expected or not secrets.compare_digest(expected, state):
        raise HTTPException(400, "Invalid Google sign-in state.")
    try:
        token_response = httpx.post("https://oauth2.googleapis.com/token", data={"code": code, "client_id": settings.google_client_id, "client_secret": settings.google_client_secret, "redirect_uri": settings.google_redirect_uri, "grant_type": "authorization_code"}, timeout=30)
        token_response.raise_for_status()
        access_token = token_response.json()["access_token"]
        profile = httpx.get("https://openidconnect.googleapis.com/v1/userinfo", headers={"Authorization": f"Bearer {access_token}"}, timeout=30)
        profile.raise_for_status()
        details = profile.json()
        email = str(details.get("email", "")).strip().lower()
        google_sub = str(details.get("sub", "")).strip()
        if not email or not google_sub:
            raise ValueError("Google did not return a verified email.")
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        raise HTTPException(400, f"Google sign-in failed: {exc}") from exc
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.google_sub == google_sub)) or db.scalar(select(User).where(User.email == email))
        if not user:
            user = User(email=email, google_sub=google_sub, role="user", email_verified_at=utc_now(), terms_accepted_at=utc_now())
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
            db.add(Subscription(workspace_id=workspace.id, plan_id=starter.id, status="trialing", trial_ends_at=utc_now() + timedelta(days=settings.trial_days)))
        else:
            user.google_sub = google_sub
            user.email_verified_at = user.email_verified_at or utc_now()
            workspace = db.scalar(select(Workspace).join(WorkspaceMembership).where(WorkspaceMembership.user_id == user.id))
        db.commit()
        request.session.clear()
        request.session.update({"authenticated": True, "user_id": user.id, "workspace_id": workspace.id})
    return RedirectResponse("/", status_code=303)


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


def _legal_page(request: Request, page_name: str):
    pages = {
        "terms": ("Terms of service", "Use TailorMail responsibly and keep your account credentials secure."),
        "privacy": ("Privacy policy", "Workspace data is isolated and used to operate the features you configure."),
        "acceptable-use": ("Acceptable use", "Do not use TailorMail for spam, unlawful profiling, harassment, or deceptive outreach."),
        "refunds": ("Refund policy", "Subscription refunds are handled according to the plan and billing terms shown at checkout."),
        "contact": ("Contact", "Contact support through your workspace support channel."),
    }
    title, body = pages[page_name]
    return templates.TemplateResponse(request, "legal.html", {"title": title, "body": body})


@app.get("/terms")
def terms_page(request: Request):
    return _legal_page(request, "terms")


@app.get("/privacy")
def privacy_page(request: Request):
    return _legal_page(request, "privacy")


@app.get("/acceptable-use")
def acceptable_use_page(request: Request):
    return _legal_page(request, "acceptable-use")


@app.get("/refunds")
def refunds_page(request: Request):
    return _legal_page(request, "refunds")


@app.get("/contact")
def contact_page(request: Request):
    return _legal_page(request, "contact")


@app.get("/robots.txt", include_in_schema=False)
def robots():
    return PlainTextResponse("User-agent: *\nDisallow: /\nAllow: /landing\nAllow: /pricing\nAllow: /terms\nAllow: /privacy\nAllow: /acceptable-use\nAllow: /refunds\nAllow: /contact\n")


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
    turnstile_token: str = Form(""),
):
    if rate_limited(request, "signup", 10):
        raise HTTPException(429, "Too many signup attempts. Try again later.")
    if not settings.signup_enabled:
        raise HTTPException(403, "New signups are currently closed.")
    if settings.turnstile_secret_key:
        try:
            challenge = httpx.post("https://challenges.cloudflare.com/turnstile/v0/siteverify", data={"secret": settings.turnstile_secret_key, "response": turnstile_token, "remoteip": request.client.host if request.client else ""}, timeout=10)
            if not challenge.json().get("success"):
                raise ValueError
        except (httpx.HTTPError, ValueError):
            return templates.TemplateResponse(request, "signup.html", {"error": "Complete the bot verification challenge.", "settings": settings}, status_code=400)
    if not terms:
        return templates.TemplateResponse(request, "signup.html", {"error": "You must accept the acceptable-use and privacy terms.", "settings": settings}, status_code=400)
    email = email.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return templates.TemplateResponse(request, "signup.html", {"error": "Enter a valid email address.", "settings": settings}, status_code=400)
    try:
        password_hash = hash_password(password)
    except ValueError as exc:
        return templates.TemplateResponse(request, "signup.html", {"error": str(exc), "settings": settings}, status_code=400)
    with SessionLocal() as db:
        if db.scalar(select(User).where(User.email == email)):
            return templates.TemplateResponse(request, "signup.html", {"error": "An account with that email already exists.", "settings": settings}, status_code=409)
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
        db.add(Subscription(workspace_id=workspace.id, plan_id=starter.id, status="trialing", trial_ends_at=utc_now() + timedelta(days=settings.trial_days)))
        db.commit()
        request.session.clear()
        request.session.update({"authenticated": True, "user_id": user.id, "workspace_id": workspace.id})
        verification = new_token()
        db.add(EmailVerificationToken(user_id=user.id, token_hash=token_hash(verification), expires_at=utc_now() + timedelta(hours=24)))
        db.commit()
        try:
            send_system_email(email, "Verify your TailorMail account", f"Verify your account: {settings.base_url}/verify-email?token={verification}")
        except Exception:
            pass
    return RedirectResponse("/", status_code=303)


@app.get("/verify-email")
def verify_email(request: Request, token: str = ""):
    if not token and request.session.get("user_id"):
        with SessionLocal() as db:
            user = db.get(User, request.session.get("user_id"))
            if user and not user.email_verified_at:
                raw = new_token()
                db.add(EmailVerificationToken(user_id=user.id, token_hash=token_hash(raw), expires_at=utc_now() + timedelta(hours=24)))
                db.commit()
                try:
                    send_system_email(user.email, "Verify your TailorMail account", f"Verify your account: {settings.base_url}/verify-email?token={raw}")
                except Exception:
                    pass
                return templates.TemplateResponse(request, "auth_message.html", {"title": "Check your email", "message": "We sent a verification link. The link expires in 24 hours."})
    with SessionLocal() as db:
        record = db.scalar(select(EmailVerificationToken).where(EmailVerificationToken.token_hash == token_hash(token), EmailVerificationToken.used_at.is_(None), EmailVerificationToken.expires_at > utc_now())) if token else None
        if not record:
            return templates.TemplateResponse(request, "auth_message.html", {"title": "Verification failed", "message": "This verification link is invalid or expired."}, status_code=400)
        user = db.get(User, record.user_id)
        user.email_verified_at = utc_now()
        record.used_at = utc_now()
        db.commit()
    return templates.TemplateResponse(request, "auth_message.html", {"title": "Email verified", "message": "Your email is verified. You can now sign in."})


@app.get("/forgot-password")
def forgot_password_page(request: Request):
    return templates.TemplateResponse(request, "forgot_password.html", {"message": ""})


@app.post("/forgot-password")
def forgot_password(request: Request, email: str = Form()):
    if rate_limited(request, "password-reset", 10):
        return templates.TemplateResponse(request, "forgot_password.html", {"message": "If that email exists, a reset link has been sent."})
    normalized = email.strip().lower()
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == normalized, User.is_active.is_(True)))
        if user:
            raw = new_token()
            db.add(PasswordResetToken(user_id=user.id, token_hash=token_hash(raw), expires_at=utc_now() + timedelta(hours=1)))
            db.commit()
            try:
                send_system_email(user.email, "Reset your TailorMail password", f"Reset your password: {settings.base_url}/reset-password?token={raw}")
            except Exception:
                pass
    return templates.TemplateResponse(request, "forgot_password.html", {"message": "If that email exists, a reset link has been sent."})


@app.get("/reset-password")
def reset_password_page(request: Request, token: str = ""):
    return templates.TemplateResponse(request, "reset_password.html", {"token": token, "error": ""})


@app.post("/reset-password")
def reset_password(request: Request, token: str = Form(), password: str = Form()):
    with SessionLocal() as db:
        record = db.scalar(select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash(token), PasswordResetToken.used_at.is_(None), PasswordResetToken.expires_at > utc_now()))
        if not record:
            return templates.TemplateResponse(request, "reset_password.html", {"token": token, "error": "This reset link is invalid or expired."}, status_code=400)
        try:
            user = db.get(User, record.user_id)
            user.password_hash = hash_password(password)
        except ValueError as exc:
            return templates.TemplateResponse(request, "reset_password.html", {"token": token, "error": str(exc)}, status_code=400)
        user.failed_login_count = 0
        user.locked_until = None
        record.used_at = utc_now()
        db.commit()
    return RedirectResponse("/login", status_code=303)


@app.post("/login")
def login(request: Request, username: str = Form(""), password: str = Form(""), next: str = Form("/"), otp: str = Form("")):
    if rate_limited(request, "login", 30):
        return templates.TemplateResponse(request, "login.html", {"next": _safe_next(next), "error": "Too many attempts. Try again later.", "mfa": False}, status_code=429)
    email = username.strip().lower()
    with SessionLocal() as db:
        pending_id = request.session.get("pending_mfa_user_id")
        if pending_id and otp:
            pending = db.get(User, pending_id)
            if pending and pending.totp_enabled and pending.totp_secret and pyotp.TOTP(decrypt_secret(pending.totp_secret)).verify(otp.strip(), valid_window=1):
                membership = db.scalar(select(WorkspaceMembership).where(WorkspaceMembership.user_id == pending.id).order_by(WorkspaceMembership.created_at))
                if membership:
                    request.session.pop("pending_mfa_user_id", None)
                    request.session.clear()
                    request.session.update({"authenticated": True, "user_id": pending.id, "workspace_id": membership.workspace_id})
                    return RedirectResponse(_safe_next(next), status_code=303)
            return templates.TemplateResponse(request, "login.html", {"next": _safe_next(next), "error": "Invalid authenticator code.", "mfa": True}, status_code=401)
        user = db.scalar(select(User).where(User.email == email))
        if user and user.locked_until and user.locked_until <= utc_now():
            user.locked_until = None
            user.failed_login_count = 0
        if user and user.password_hash and user.is_active and (not user.locked_until or user.locked_until <= utc_now()) and verify_password(user.password_hash, password):
            user.failed_login_count = 0
            user.last_login_at = utc_now()
            membership = db.scalar(select(WorkspaceMembership).where(WorkspaceMembership.user_id == user.id).order_by(WorkspaceMembership.created_at))
            if membership:
                if user.totp_enabled:
                    request.session["pending_mfa_user_id"] = user.id
                    return RedirectResponse(f"/login?mfa=1&next={quote(_safe_next(next), safe='/?=&')}", status_code=303)
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
        jobs = db.scalars(select(OutreachJob).where(OutreachJob.workspace_id == workspace.id)).all()
        invoices = db.scalars(select(Invoice).where(Invoice.workspace_id == workspace.id)).all()
    return JSONResponse({"user": {"id": user.id, "email": user.email}, "workspace": {"id": workspace.id, "name": workspace.name}, "candidates": [{"id": item.id, "name": item.name, "email": item.email, "cv_yaml": item.cv_yaml} for item in candidates], "campaigns": [{"id": item.id, "company_name": item.company_name, "recipient_email": item.recipient_email, "status": item.status, "email_subject": item.email_subject, "email_body": item.email_body} for item in campaigns], "jobs": [{"id": item.id, "type": item.job_type, "status": item.status, "created_at": item.created_at.isoformat()} for item in jobs], "invoices": [{"id": item.id, "status": item.status, "amount_cents": item.amount_cents, "currency": item.currency} for item in invoices]})


@app.post("/account/password")
def change_password(request: Request, current_password: str = Form(), new_password: str = Form()):
    with SessionLocal() as db:
        _, user = current_workspace(request, db)
        if not user.password_hash or not verify_password(user.password_hash, current_password):
            raise HTTPException(400, "Current password is incorrect.")
        user.password_hash = hash_password(new_password)
        db.commit()
    return RedirectResponse("/?tab=settings", status_code=303)


@app.post("/account/email")
def change_email(request: Request, email: str = Form()):
    normalized = email.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", normalized):
        raise HTTPException(400, "Enter a valid email address.")
    with SessionLocal() as db:
        _, user = current_workspace(request, db)
        if db.scalar(select(User).where(User.email == normalized, User.id != user.id)):
            raise HTTPException(409, "That email is already in use.")
        user.email = normalized
        user.email_verified_at = None
        raw = new_token()
        db.add(EmailVerificationToken(user_id=user.id, token_hash=token_hash(raw), expires_at=utc_now() + timedelta(hours=24)))
        db.commit()
        try:
            send_system_email(normalized, "Verify your new TailorMail email", f"Verify your new address: {settings.base_url}/verify-email?token={raw}")
        except Exception:
            pass
    return RedirectResponse("/verify-email", status_code=303)


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
    try:
        if abs(int(timestamp) - int(datetime.now(timezone.utc).timestamp())) > 300:
            raise HTTPException(400, "Expired Stripe signature.")
    except ValueError as exc:
        raise HTTPException(400, "Invalid Stripe signature.") from exc
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
        provider_subscription_id = data.get("subscription") if event_type.startswith("invoice.") else data.get("id")
        subscription = db.scalar(select(Subscription).where(Subscription.workspace_id == int(workspace_id))) if workspace_id and str(workspace_id).isdigit() else None
        if not subscription and provider_subscription_id:
            subscription = db.scalar(select(Subscription).where(Subscription.provider_subscription_id == provider_subscription_id))
        if subscription:
            if event_type in {"customer.subscription.created", "customer.subscription.updated"}:
                subscription.status = data.get("status", subscription.status)
                subscription.provider_customer_id = data.get("customer") or subscription.provider_customer_id
                subscription.provider_subscription_id = data.get("id") or subscription.provider_subscription_id
                if data.get("current_period_end"):
                    subscription.current_period_end = datetime.fromtimestamp(data["current_period_end"], timezone.utc)
            elif event_type == "customer.subscription.deleted":
                subscription.status = "canceled"
            elif event_type == "invoice.payment_failed":
                subscription.status = "past_due"
                subscription.grace_ends_at = utc_now() + timedelta(days=7)
            elif event_type == "invoice.paid":
                subscription.status = "active"
            if event_type.startswith("invoice.") and subscription:
                db.merge(Invoice(workspace_id=subscription.workspace_id, provider_invoice_id=data.get("id"), status=data.get("status", event_type.removeprefix("invoice.")), amount_cents=int(data.get("amount_paid") or data.get("amount_due") or 0), currency=data.get("currency", "gbp")))
        db.commit()
    return {"ok": True}


@app.get("/billing")
def billing_page(request: Request, success: int = 0):
    with SessionLocal() as db:
        workspace, user = current_workspace(request, db)
        subscription = status_for(db, workspace)
        plans = db.scalars(select(Plan).where(Plan.active.is_(True)).order_by(Plan.id)).all()
        invoices = db.scalars(select(Invoice).where(Invoice.workspace_id == workspace.id).order_by(Invoice.created_at.desc())).all()
    return templates.TemplateResponse(request, "billing.html", {"status": subscription, "plans": plans, "invoices": invoices, "success": bool(success)})


@app.post("/billing/checkout")
def billing_checkout(request: Request, plan_slug: str = Form(), interval: str = Form("monthly")):
    if not settings.stripe_secret_key:
        raise HTTPException(503, "Stripe billing is not configured.")
    if interval not in {"monthly", "yearly"}:
        raise HTTPException(400, "Invalid billing interval.")
    with SessionLocal() as db:
        workspace, user = current_workspace(request, db)
        plan = db.scalar(select(Plan).where(Plan.slug == plan_slug, Plan.active.is_(True)))
        if not plan:
            raise HTTPException(404, "Plan not found.")
        price = plan.stripe_monthly_price_id if interval == "monthly" else plan.stripe_yearly_price_id
        if not price:
            raise HTTPException(503, "This plan has no Stripe price configured yet.")
        data = {"mode": "subscription", "success_url": f"{settings.base_url}/billing?success=1", "cancel_url": f"{settings.base_url}/billing", "line_items[0][price]": price, "line_items[0][quantity]": "1", "customer_email": user.email, "metadata[workspace_id]": str(workspace.id), "subscription_data[metadata][workspace_id]": str(workspace.id)}
    response = httpx.post("https://api.stripe.com/v1/checkout/sessions", data=data, auth=(settings.stripe_secret_key, ""), timeout=30)
    if response.status_code >= 400:
        raise HTTPException(502, "Stripe checkout could not be created.")
    return RedirectResponse(response.json().get("url", "/billing"), status_code=303)


@app.get("/billing/portal")
def billing_portal(request: Request):
    if not settings.stripe_secret_key:
        raise HTTPException(503, "Stripe billing is not configured.")
    with SessionLocal() as db:
        workspace, user = current_workspace(request, db)
        subscription = db.scalar(select(Subscription).where(Subscription.workspace_id == workspace.id).order_by(Subscription.id.desc()))
        if not subscription or not subscription.provider_customer_id:
            raise HTTPException(400, "No Stripe customer is linked to this workspace yet.")
        data = {"customer": subscription.provider_customer_id, "return_url": f"{settings.base_url}/billing"}
    response = httpx.post("https://api.stripe.com/v1/billing_portal/sessions", data=data, auth=(settings.stripe_secret_key, ""), timeout=30)
    if response.status_code >= 400:
        raise HTTPException(502, "Stripe portal could not be opened.")
    return RedirectResponse(response.json().get("url", "/billing"), status_code=303)


@app.post("/account/delete")
def delete_account(request: Request):
    with SessionLocal() as db:
        workspace, user = current_workspace(request, db)
        for campaign in db.scalars(select(Campaign).where(workspace_filter(Campaign, workspace))).all():
            if campaign.artifact_path:
                try:
                    Path(campaign.artifact_path).unlink(missing_ok=True)
                except OSError:
                    logging.getLogger("tailormail").warning("Could not remove campaign artifact during deletion", exc_info=True)
            db.delete(campaign)
        for candidate in db.scalars(select(Candidate).where(workspace_filter(Candidate, workspace))).all():
            db.delete(candidate)
        for model in (EmailAccount, AIProviderConfig, Notification, SupportTicket, Subscription, Invoice, UsageRecord, OutreachJob, AuditEvent, AuditLog):
            for item in db.scalars(select(model).where(model.workspace_id == workspace.id)).all():
                db.delete(item)
        for item in db.scalars(select(Suppression).where(Suppression.workspace_id == workspace.id)).all():
            db.delete(item)
        for membership in db.scalars(select(WorkspaceMembership).where(WorkspaceMembership.workspace_id == workspace.id)).all():
            db.delete(membership)
        for token_model in (SessionToken, PasswordResetToken, EmailVerificationToken):
            for token in db.scalars(select(token_model).where(token_model.user_id == user.id)).all():
                db.delete(token)
        db.delete(workspace)
        user.is_active = False
        user.email = f"deleted-{user.id}@invalid.local"
        user.password_hash = None
        user.google_sub = None
        db.commit()
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/settings/2fa/setup")
def setup_2fa(request: Request):
    with SessionLocal() as db:
        _, user = current_workspace(request, db)
        secret = pyotp.random_base32()
        request.session["pending_totp_secret"] = secret
        return JSONResponse({"issuer": "TailorMail", "account": user.email, "otpauth_uri": pyotp.TOTP(secret).provisioning_uri(user.email, issuer_name="TailorMail")})


@app.post("/settings/2fa/enable")
def enable_2fa(request: Request, code: str = Form()):
    secret = request.session.get("pending_totp_secret")
    if not secret or not pyotp.TOTP(secret).verify(code.strip(), valid_window=1):
        raise HTTPException(400, "Invalid authenticator code.")
    with SessionLocal() as db:
        _, user = current_workspace(request, db)
        user.totp_secret = encrypt_secret(secret)
        user.totp_enabled = True
        db.commit()
    request.session.pop("pending_totp_secret", None)
    return RedirectResponse("/?tab=settings", status_code=303)


@app.post("/settings/2fa/disable")
def disable_2fa(request: Request, code: str = Form()):
    with SessionLocal() as db:
        _, user = current_workspace(request, db)
        if not user.totp_enabled or not user.totp_secret or not pyotp.TOTP(decrypt_secret(user.totp_secret)).verify(code.strip(), valid_window=1):
            raise HTTPException(400, "Invalid authenticator code.")
        user.totp_enabled = False
        user.totp_secret = None
        db.commit()
    return RedirectResponse("/?tab=settings", status_code=303)


@app.post("/jobs/daily-outreach")
def enqueue_daily_outreach(request: Request):
    # Queue work and return immediately; durable job state is visible to the dashboard.
    with SessionLocal() as db:
        workspace, user = current_workspace(request, db)
    job_id = enqueue_job("daily_outreach", {"requested_at": utc_now().isoformat()}, workspace.id, user.id)
    return JSONResponse({"queued": True, "job_id": job_id})


@app.get("/jobs/{job_id}")
def job_status(request: Request, job_id: int):
    with SessionLocal() as db:
        workspace, _ = current_workspace(request, db)
        job = db.scalar(select(OutreachJob).where(OutreachJob.id == job_id, OutreachJob.workspace_id == workspace.id))
        if not job:
            raise HTTPException(404, "Job not found.")
        return JSONResponse({"id": job.id, "type": job.job_type, "status": job.status, "progress": job.progress, "attempts": job.attempts, "error": job.error, "created_at": job.created_at.isoformat(), "started_at": job.started_at.isoformat() if job.started_at else None, "finished_at": job.finished_at.isoformat() if job.finished_at else None})


@app.post("/jobs/{job_id}/cancel")
def cancel_job(request: Request, job_id: int):
    with SessionLocal() as db:
        workspace, _ = current_workspace(request, db)
        job = db.scalar(select(OutreachJob).where(OutreachJob.id == job_id, OutreachJob.workspace_id == workspace.id))
        if not job:
            raise HTTPException(404, "Job not found.")
        if job.status in {"queued", "running"}:
            job.status = "cancelled"
            job.finished_at = utc_now()
            db.commit()
    return RedirectResponse("/?tab=settings", status_code=303)


@app.post("/notifications/{notification_id}/read")
def mark_notification_read(request: Request, notification_id: int):
    with SessionLocal() as db:
        workspace, _ = current_workspace(request, db)
        notification = db.scalar(select(Notification).where(Notification.id == notification_id, Notification.workspace_id == workspace.id))
        if not notification:
            raise HTTPException(404, "Notification not found.")
        notification.read_at = utc_now()
        db.commit()
    return RedirectResponse("/", status_code=303)


@app.get("/unsubscribe")
def unsubscribe(email: str, workspace_id: int | None = None):
    normalized = email.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", normalized):
        raise HTTPException(400, "A valid email address is required.")
    with SessionLocal() as db:
        # Public opt-outs are global by design; callers cannot suppress an arbitrary tenant by guessing an ID.
        db.add(Suppression(workspace_id=None, email=normalized, reason="unsubscribe"))
        db.commit()
    return PlainTextResponse("You have been unsubscribed.")


def audit(db, campaign: Campaign, action: str, detail: str, user_id: int | None = None) -> None:
    db.add(AuditEvent(campaign_id=campaign.id, workspace_id=campaign.workspace_id, user_id=user_id or campaign.user_id, action=action, detail=detail))


PERSONAL_EMAIL_DOMAINS = {"gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com", "aol.com", "proton.me", "protonmail.com"}


def corporate_recipient(email: str) -> bool:
    return "@" in email and email.rsplit("@", 1)[-1].lower() not in PERSONAL_EMAIL_DOMAINS


def remaining_daily_send_budget(db, workspace_id: int | None = None) -> int:
    workspace = db.get(Workspace, workspace_id) if workspace_id is not None else None
    timezone_name = workspace.timezone if workspace else settings.outreach_timezone
    try:
        local_timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("OUTREACH_TIMEZONE must be a valid IANA timezone.") from exc
    now = datetime.now(local_timezone)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    filters = [Campaign.status == "sent", Campaign.sent_at >= start]
    if workspace_id is not None:
        filters.append(Campaign.workspace_id == workspace_id)
    sent_today = db.scalar(select(func.count(Campaign.id)).where(*filters)) or 0
    daily_limit = workspace.daily_limit if workspace else settings.outreach_daily_limit
    if workspace:
        plan_limit = int(status_for(db, workspace)["limits"].get("daily_emails", daily_limit))
        daily_limit = min(daily_limit, plan_limit)
        age_days = max((utc_now() - workspace.created_at).days, 0)
        if age_days < 7:
            daily_limit = min(daily_limit, max(5, (age_days + 1) * 5))
    return max(daily_limit - sent_today, 0)


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
        notifications = db.scalars(select(Notification).where(Notification.workspace_id == workspace.id, Notification.read_at.is_(None)).order_by(Notification.created_at.desc()).limit(20)).all()
        blocklist = db.scalars(select(Suppression).where(Suppression.workspace_id == workspace.id, Suppression.reason == "workspace_blocklist").order_by(Suppression.created_at.desc())).all()
        jobs = db.scalars(select(OutreachJob).where(OutreachJob.workspace_id == workspace.id).order_by(OutreachJob.created_at.desc()).limit(10)).all()
        email_account = db.scalar(select(EmailAccount).where(EmailAccount.workspace_id == workspace.id, EmailAccount.status == "active").order_by(EmailAccount.id))
        ai_config = db.scalar(select(AIProviderConfig).where(AIProviderConfig.workspace_id == workspace.id))
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "campaigns": campaigns,
                "candidates": candidates,
                "counts": counts,
                "daily_sent": (workspace.daily_limit if workspace else settings.outreach_daily_limit) - daily_remaining,
                "daily_remaining": daily_remaining,
                "active_tab": tab,
                "settings": settings,
                "subscription_status": subscription_status,
                "workspace": workspace,
                "notifications": notifications,
                "impersonating": bool(request.session.get("impersonator_id")),
                "email_account": email_account,
                "ai_config": ai_config,
                "blocklist": blocklist,
                "jobs": jobs,
            },
        )


@app.get("/onboarding")
def onboarding(request: Request):
    with SessionLocal() as db:
        workspace, user = current_workspace(request, db)
        candidate_count = db.scalar(select(func.count(Candidate.id)).where(workspace_filter(Candidate, workspace), Candidate.deleted_at.is_(None))) or 0
        ai_configured = bool(db.scalar(select(AIProviderConfig).where(AIProviderConfig.workspace_id == workspace.id)))
        sender_configured = bool(db.scalar(select(EmailAccount).where(EmailAccount.workspace_id == workspace.id, EmailAccount.status == "active")))
    return templates.TemplateResponse(request, "onboarding.html", {"workspace": workspace, "user": user, "candidate_count": candidate_count, "ai_configured": ai_configured, "sender_configured": sender_configured})


@app.get("/sent")
def sent_emails(request: Request):
    with SessionLocal() as db:
        workspace, _ = current_workspace(request, db)
        campaigns = db.scalars(select(Campaign).where(workspace_filter(Campaign, workspace), Campaign.status == "sent", Campaign.deleted_at.is_(None)).order_by(Campaign.sent_at.desc())).all()
    return templates.TemplateResponse(request, "sent.html", {"campaigns": campaigns})


@app.get("/leads")
def leads_page(request: Request):
    with SessionLocal() as db:
        workspace, _ = current_workspace(request, db)
        campaigns = db.scalars(select(Campaign).where(workspace_filter(Campaign, workspace), Campaign.deleted_at.is_(None)).order_by(Campaign.created_at.desc())).all()
    return templates.TemplateResponse(request, "leads.html", {"campaigns": campaigns})


@app.get("/support")
def support_page(request: Request):
    with SessionLocal() as db:
        workspace, user = current_workspace(request, db)
        tickets = db.scalars(select(SupportTicket).where(SupportTicket.workspace_id == workspace.id, SupportTicket.user_id == user.id).order_by(SupportTicket.created_at.desc())).all()
    return templates.TemplateResponse(request, "support.html", {"tickets": tickets, "error": ""})


@app.post("/support")
def create_support_ticket(request: Request, subject: str = Form(), body: str = Form()):
    if not subject.strip() or not body.strip():
        raise HTTPException(400, "Subject and message are required.")
    with SessionLocal() as db:
        workspace, user = current_workspace(request, db)
        db.add(SupportTicket(workspace_id=workspace.id, user_id=user.id, subject=subject.strip()[:300], body=body.strip()[:10000]))
        db.commit()
    return RedirectResponse("/support", status_code=303)


@app.get("/admin")
def admin_dashboard(request: Request):
    with SessionLocal() as db:
        _, user = current_workspace(request, db)
        if user.role not in {"admin", "superadmin"}:
            raise HTTPException(403, "Administrator access required.")
        return templates.TemplateResponse(
            request,
            "admin.html",
            {
                "users": db.scalars(select(User).order_by(User.created_at.desc())).all(),
                "workspaces": db.scalars(select(Workspace).where(Workspace.deleted_at.is_(None)).order_by(Workspace.created_at.desc())).all(),
                "campaign_count": db.scalar(select(func.count(Campaign.id))) or 0,
                "sent_count": db.scalar(select(func.count(Campaign.id)).where(Campaign.status == "sent")) or 0,
                "audit_count": db.scalar(select(func.count(AuditLog.id))) or 0,
                "kill_switch": kill_switch_enabled(),
            },
        )


def _require_admin(request: Request, db):
    _, user = current_workspace(request, db)
    if user.role not in {"admin", "superadmin"}:
        raise HTTPException(403, "Administrator access required.")
    return user


@app.get("/admin/users")
def admin_users(request: Request, q: str = ""):
    with SessionLocal() as db:
        _require_admin(request, db)
        query = select(User).order_by(User.created_at.desc())
        if q.strip():
            query = query.where(User.email.ilike(f"%{q.strip()}%"))
        users = db.scalars(query).all()
        return JSONResponse({"users": [{"id": u.id, "email": u.email, "role": u.role, "active": u.is_active, "verified": bool(u.email_verified_at), "created_at": u.created_at.isoformat()} for u in users]})


@app.get("/admin/metrics")
def admin_metrics(request: Request):
    with SessionLocal() as db:
        _require_admin(request, db)
        status_counts = dict(db.execute(select(Campaign.status, func.count(Campaign.id)).group_by(Campaign.status)).all())
        subscriptions = db.scalars(select(Subscription)).all()
        jobs = dict(db.execute(select(OutreachJob.status, func.count(OutreachJob.id)).group_by(OutreachJob.status)).all())
        return JSONResponse({"users": db.scalar(select(func.count(User.id))) or 0, "workspaces": db.scalar(select(func.count(Workspace.id)).where(Workspace.deleted_at.is_(None))) or 0, "active_subscriptions": sum(1 for item in subscriptions if item.status == "active"), "trialing": sum(1 for item in subscriptions if item.status == "trialing"), "sent": status_counts.get("sent", 0), "failed": status_counts.get("failed", 0) + status_counts.get("send_uncertain", 0), "campaign_status": status_counts, "job_status": jobs, "kill_switch": kill_switch_enabled()})


@app.get("/admin/email-log")
def admin_email_log(request: Request, limit: int = 100):
    with SessionLocal() as db:
        _require_admin(request, db)
        campaigns = db.scalars(select(Campaign).where(Campaign.status.in_(["sent", "send_uncertain"])).order_by(Campaign.sent_at.desc(), Campaign.created_at.desc()).limit(min(max(limit, 1), 1000))).all()
        return JSONResponse({"emails": [{"id": c.id, "workspace_id": c.workspace_id, "company": c.company_name, "recipient": c.recipient_email, "subject": c.email_subject, "status": c.status, "sent_at": c.sent_at.isoformat() if c.sent_at else None} for c in campaigns]})


@app.post("/admin/users/{user_id}/status")
def admin_user_status(request: Request, user_id: int, active: bool = Form()):
    with SessionLocal() as db:
        actor = _require_admin(request, db)
        target = db.get(User, user_id)
        if not target:
            raise HTTPException(404, "User not found.")
        if target.id == actor.id and not active:
            raise HTTPException(400, "You cannot disable your own account.")
        target.is_active = active
        db.add(AuditLog(workspace_id=None, user_id=actor.id, action="user_status_changed", entity_type="user", entity_id=target.id, detail={"active": active}, ip_address=request.client.host if request.client else None))
        db.commit()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/users/{user_id}/role")
def admin_user_role(request: Request, user_id: int, role: str = Form()):
    if role not in {"user", "admin", "superadmin"}:
        raise HTTPException(400, "Invalid role.")
    with SessionLocal() as db:
        actor = _require_admin(request, db)
        if actor.role != "superadmin" and role == "superadmin":
            raise HTTPException(403, "Only superadmins can grant superadmin access.")
        target = db.get(User, user_id)
        if not target:
            raise HTTPException(404, "User not found.")
        target.role = role
        db.add(AuditLog(workspace_id=None, user_id=actor.id, action="user_role_changed", entity_type="user", entity_id=target.id, detail={"role": role}, ip_address=request.client.host if request.client else None))
        db.commit()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/users/{user_id}/verify")
def admin_user_verify(request: Request, user_id: int):
    with SessionLocal() as db:
        actor = _require_admin(request, db)
        target = db.get(User, user_id)
        if not target:
            raise HTTPException(404, "User not found.")
        target.email_verified_at = target.email_verified_at or utc_now()
        db.add(AuditLog(workspace_id=None, user_id=actor.id, action="user_verified", entity_type="user", entity_id=target.id, detail={}))
        db.commit()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/users/{user_id}/force-logout")
def admin_force_logout(request: Request, user_id: int):
    with SessionLocal() as db:
        actor = _require_admin(request, db)
        target = db.get(User, user_id)
        if not target:
            raise HTTPException(404, "User not found.")
        db.query(SessionToken).filter(SessionToken.user_id == target.id, SessionToken.revoked_at.is_(None)).update({SessionToken.revoked_at: utc_now()})
        db.add(AuditLog(workspace_id=None, user_id=actor.id, action="user_force_logout", entity_type="user", entity_id=target.id, detail={}))
        db.commit()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/impersonate/{user_id}")
def admin_impersonate(request: Request, user_id: int):
    with SessionLocal() as db:
        actor = _require_admin(request, db)
        target = db.get(User, user_id)
        membership = db.scalar(select(WorkspaceMembership).where(WorkspaceMembership.user_id == user_id).order_by(WorkspaceMembership.created_at)) if target else None
        if not target or not membership or not target.is_active:
            raise HTTPException(404, "Active user workspace not found.")
        request.session["impersonator_id"] = actor.id
        request.session.update({"authenticated": True, "user_id": target.id, "workspace_id": membership.workspace_id})
        db.add(AuditLog(workspace_id=membership.workspace_id, user_id=actor.id, action="impersonation_started", entity_type="user", entity_id=target.id, detail={}))
        db.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/admin/stop-impersonation")
def stop_impersonation(request: Request):
    actor_id = request.session.get("impersonator_id")
    if not actor_id:
        return RedirectResponse("/", status_code=303)
    with SessionLocal() as db:
        actor = db.get(User, actor_id)
        if not actor or actor.role not in {"admin", "superadmin"}:
            request.session.clear()
            return RedirectResponse("/login", status_code=303)
        workspace = db.scalar(select(Workspace).join(WorkspaceMembership).where(WorkspaceMembership.user_id == actor.id).order_by(WorkspaceMembership.created_at))
        db.add(AuditLog(workspace_id=workspace.id if workspace else None, user_id=actor.id, action="impersonation_stopped", entity_type="user", entity_id=request.session.get("user_id"), detail={}))
        db.commit()
    request.session.update({"authenticated": True, "user_id": actor.id, "workspace_id": workspace.id if workspace else None})
    request.session.pop("impersonator_id", None)
    return RedirectResponse("/admin", status_code=303)


@app.get("/admin/audit.csv")
def admin_audit_csv(request: Request):
    with SessionLocal() as db:
        _require_admin(request, db)
        rows = db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(10000)).all()
    lines = ["id,user_id,action,entity_type,entity_id,created_at,detail"]
    for row in rows:
        detail = json.dumps(row.detail or {}, ensure_ascii=False).replace('"', '""')
        lines.append(f'{row.id},{row.user_id or ""},{row.action},{row.entity_type or ""},{row.entity_id or ""},{row.created_at.isoformat()},"{detail}"')
    return PlainTextResponse("\n".join(lines), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=audit.csv"})


@app.get("/admin/support")
def admin_support(request: Request):
    with SessionLocal() as db:
        _require_admin(request, db)
        tickets = db.scalars(select(SupportTicket).order_by(SupportTicket.created_at.desc()).limit(500)).all()
        return JSONResponse({"tickets": [{"id": t.id, "workspace_id": t.workspace_id, "user_id": t.user_id, "subject": t.subject, "body": t.body, "status": t.status, "created_at": t.created_at.isoformat()} for t in tickets]})


@app.post("/admin/support/{ticket_id}/status")
def admin_support_status(request: Request, ticket_id: int, status: str = Form()):
    if status not in {"open", "pending", "resolved", "closed"}:
        raise HTTPException(400, "Invalid ticket status.")
    with SessionLocal() as db:
        actor = _require_admin(request, db)
        ticket = db.get(SupportTicket, ticket_id)
        if not ticket:
            raise HTTPException(404, "Ticket not found.")
        ticket.status = status
        db.add(AuditLog(workspace_id=ticket.workspace_id, user_id=actor.id, action="support_ticket_status_changed", entity_type="support_ticket", entity_id=ticket.id, detail={"status": status}))
        db.commit()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/suppression")
def admin_add_suppression(request: Request, email: str = Form(""), domain: str = Form(""), reason: str = Form("admin")):
    if not email.strip() and not domain.strip():
        raise HTTPException(400, "Email or domain is required.")
    with SessionLocal() as db:
        actor = _require_admin(request, db)
        db.add(Suppression(workspace_id=None, email=email.strip().lower() or None, domain=domain.strip().lower() or None, reason=reason.strip()[:80] or "admin"))
        db.add(AuditLog(workspace_id=None, user_id=actor.id, action="global_suppression_added", entity_type="suppression", detail={"email": bool(email.strip()), "domain": bool(domain.strip())}, ip_address=request.client.host if request.client else None))
        db.commit()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/workspaces/{workspace_id}/pause")
def admin_pause_workspace(request: Request, workspace_id: int, paused: bool = Form()):
    with SessionLocal() as db:
        actor = _require_admin(request, db)
        workspace = db.get(Workspace, workspace_id)
        if not workspace:
            raise HTTPException(404, "Workspace not found.")
        workspace.paused = paused
        db.add(AuditLog(workspace_id=workspace.id, user_id=actor.id, action="workspace_pause_changed", entity_type="workspace", entity_id=workspace.id, detail={"paused": paused}, ip_address=request.client.host if request.client else None))
        db.commit()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/workspaces/{workspace_id}/trial")
def admin_extend_trial(request: Request, workspace_id: int, days: int = Form()):
    if not -365 <= days <= 365:
        raise HTTPException(400, "Trial adjustment must be between -365 and 365 days.")
    with SessionLocal() as db:
        actor = _require_admin(request, db)
        subscription = db.scalar(select(Subscription).where(Subscription.workspace_id == workspace_id).order_by(Subscription.id.desc()))
        if not subscription:
            raise HTTPException(404, "Subscription not found.")
        subscription.trial_ends_at = (subscription.trial_ends_at or utc_now()) + timedelta(days=days)
        subscription.status = "trialing"
        db.add(AuditLog(workspace_id=workspace_id, user_id=actor.id, action="trial_adjusted", entity_type="workspace", entity_id=workspace_id, detail={"days": days}, ip_address=request.client.host if request.client else None))
        db.commit()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/plans/{plan_id}")
def admin_update_plan(request: Request, plan_id: int, limits_json: str = Form(), monthly_price_cents: int = Form(0), yearly_price_cents: int = Form(0), active: bool = Form(True)):
    with SessionLocal() as db:
        actor = _require_admin(request, db)
        plan = db.get(Plan, plan_id)
        if not plan:
            raise HTTPException(404, "Plan not found.")
        try:
            limits = json.loads(limits_json)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, "limits_json must be valid JSON.") from exc
        if not isinstance(limits, dict) or any(not isinstance(value, (bool, int, float)) for value in limits.values()):
            raise HTTPException(400, "Plan limits must be a JSON object of numeric/boolean values.")
        plan.limits = limits
        plan.monthly_price_cents = max(monthly_price_cents, 0)
        plan.yearly_price_cents = max(yearly_price_cents, 0)
        plan.active = active
        db.add(AuditLog(workspace_id=None, user_id=actor.id, action="plan_updated", entity_type="plan", entity_id=plan.id, detail={"slug": plan.slug}))
        db.commit()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/system/kill-switch")
def admin_kill_switch(request: Request, enabled: bool = Form()):
    with SessionLocal() as db:
        actor = _require_admin(request, db)
        update_settings(outreach_kill_switch=enabled)
        set_setting(db, "outreach_kill_switch", "true" if enabled else "false")
        db.add(AuditLog(workspace_id=None, user_id=actor.id, action="kill_switch_changed", entity_type="system", detail={"enabled": enabled}, ip_address=request.client.host if request.client else None))
        db.commit()
    return RedirectResponse("/admin", status_code=303)


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
    workspace_id: int | None = None,
    candidate_id: int = Form(),
    company_name: str = Form(),
    company_url: str = Form(""),
    target_role: str = Form(""),
    job_description: str = Form(""),
    recipient_email: str = Form(),
    auto_prepare: bool = Form(False),
):
    with SessionLocal() as db:
        if request:
            workspace, user = current_workspace(request, db)
        elif workspace_id:
            workspace = db.get(Workspace, workspace_id)
            user = db.get(User, workspace.owner_user_id) if workspace else None
            if not workspace or not user:
                raise HTTPException(404, "Workspace not found")
        else:
            workspace, user = ensure_default_workspace(db)
        require_feature(db, workspace, "campaigns")
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
        return prepare_campaign(campaign_id, request=request, workspace_id=workspace_id)

    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@app.post("/leads/discover")
def discover_lead(request: Request = None, workspace_id: int | None = None, candidate_id: int = Form(), query: str = Form(), target_role: str = Form("")):
    with SessionLocal() as db:
        if request:
            workspace, _ = current_workspace(request, db)
        elif workspace_id:
            workspace = db.get(Workspace, workspace_id)
        else:
            workspace, _ = ensure_default_workspace(db)
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
        workspace_id=workspace_id,
    )


@app.post("/leads/batch")
def discover_lead_batch(
    request: Request = None,
    workspace_id: int | None = None,
    candidate_id: int = Form(),
    query: str = Form(),
    target_role: str = Form(""),
    limit: int = Form(100),
    send_when_ready: bool = Form(False),
):
    limit = min(max(limit, 1), 100)
    with SessionLocal() as db:
        if request:
            workspace, _ = current_workspace(request, db)
        elif workspace_id:
            workspace = db.get(Workspace, workspace_id)
        else:
            workspace, _ = ensure_default_workspace(db)
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
                workspace_id=workspace_id,
            )
            if send_when_ready:
                campaign_id = int(response.headers["location"].rsplit("/", 1)[-1])
                send_campaign(campaign_id, approved=True, request=request, workspace_id=workspace_id)
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
def prepare_campaign(campaign_id: int, request: Request = None, workspace_id: int | None = None):
    with SessionLocal() as db:
        if request:
            workspace, _ = current_workspace(request, db)
        elif workspace_id:
            workspace = db.get(Workspace, workspace_id)
        else:
            workspace, _ = ensure_default_workspace(db)
        require_feature(db, workspace, "ai_tailoring")
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
            db.add(UsageRecord(workspace_id=workspace.id, user_id=campaign.user_id, metric="ai_requests", quantity=1, period_start=utc_now().replace(hour=0, minute=0, second=0, microsecond=0)))

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
def send_campaign(campaign_id: int, approved: bool = Form(False), request: Request = None, workspace_id: int | None = None):
    if not approved:
        raise HTTPException(400, "Explicit approval is required to send.")
    if kill_switch_enabled():
        raise HTTPException(503, "Outreach sending is temporarily paused by the administrator.")
    with SessionLocal() as db:
        if request:
            workspace, user = current_workspace(request, db)
        elif workspace_id:
            workspace = db.get(Workspace, workspace_id)
            user = db.get(User, workspace.owner_user_id) if workspace else None
            if not workspace or not user:
                raise HTTPException(404, "Workspace not found")
        else:
            workspace, user = ensure_default_workspace(db)
        require_send_slot(db, workspace)
        recipient = campaign_recipient = db.scalar(select(Campaign.recipient_email).where(Campaign.id == campaign_id, Campaign.workspace_id == workspace.id))
        recipient_domain = recipient.rsplit("@", 1)[-1].lower() if recipient and "@" in recipient else ""
        if not corporate_recipient(recipient or ""):
            raise HTTPException(400, "Only verified corporate addresses can receive outreach.")
        suppressed = db.scalar(select(Suppression).where(Suppression.email == (recipient or "").lower(), (Suppression.workspace_id == workspace.id) | Suppression.workspace_id.is_(None))) or db.scalar(select(Suppression).where(Suppression.domain == recipient_domain, (Suppression.workspace_id == workspace.id) | Suppression.workspace_id.is_(None)))
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
        sender_account = db.scalar(select(EmailAccount).where(EmailAccount.workspace_id == workspace.id, EmailAccount.status == "active").order_by(EmailAccount.id))
        readiness_error = cv_outreach_readiness_error(campaign.candidate.cv_yaml)
        if readiness_error or not campaign.research or not campaign.research.get("sources"):
            readiness_error = readiness_error or "Company research and a public source are required before sending."
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
                email_account=sender_account,
            )
            campaign.status = "sent"
            campaign.sent_at = datetime.now(timezone.utc)
            detail = f"One email sent to {campaign.recipient_email}."
            if provider_message_id:
                detail += f" Provider message ID: {provider_message_id}."
            audit(db, campaign, "sent", detail, user.id)
            db.add(Notification(workspace_id=workspace.id, user_id=user.id, kind="campaign_sent", message=f"Application sent to {campaign.recipient_email}."))
            db.commit()
        except Exception as exc:
            campaign.status = "send_uncertain"
            if sender_account and ("401" in str(exc) or "403" in str(exc) or "authentication" in str(exc).lower() or "login" in str(exc).lower()):
                sender_account.status = "reauth_required" if sender_account.provider == "gmail" else "paused"
            audit(db, campaign, "send_uncertain", str(exc))
            db.add(Notification(workspace_id=workspace.id, user_id=user.id, kind="send_error", message=f"Delivery failed for {campaign.recipient_email}."))
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
    outreach_schedule_time: str = Form("09:00"),
):
    with SessionLocal() as db:
        workspace, user = current_workspace(request, db)
        try:
            ZoneInfo(outreach_timezone.strip() or workspace.timezone)
        except ZoneInfoNotFoundError as exc:
            raise HTTPException(400, "Daily schedule timezone must be a valid IANA timezone.") from exc
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", outreach_schedule_time.strip()):
            raise HTTPException(400, "Daily schedule time must use HH:MM format.")
        workspace.timezone = outreach_timezone.strip() or workspace.timezone
        workspace.schedule_time = outreach_schedule_time.strip()
        workspace.daily_limit = min(max(outreach_daily_limit, 1), 100)
        workspace.outreach_candidate_id = max(outreach_candidate_id, 0) or None
        workspace.outreach_query = outreach_query.strip()
        workspace.outreach_target_role = outreach_target_role.strip()
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
        db.commit()
    update_settings(
        base_url=(base_url.strip() or settings.base_url) if user.role in {"admin", "superadmin"} else settings.base_url,
        api_key=(api_key.strip() or settings.api_key) if user.role in {"admin", "superadmin"} else settings.api_key,
        llm_model=(llm_model.strip() or settings.llm_model) if user.role in {"admin", "superadmin"} else settings.llm_model,
        smtp_host=(smtp_host.strip() or settings.smtp_host) if user.role in {"admin", "superadmin"} else settings.smtp_host,
        smtp_port=smtp_port if user.role in {"admin", "superadmin"} else settings.smtp_port,
        smtp_username=(smtp_username.strip() or settings.smtp_username) if user.role in {"admin", "superadmin"} else settings.smtp_username,
        smtp_password=(smtp_password.strip() or settings.smtp_password) if user.role in {"admin", "superadmin"} else settings.smtp_password,
        smtp_from=(smtp_from.strip() or settings.smtp_from) if user.role in {"admin", "superadmin"} else settings.smtp_from,
        smtp_use_tls=smtp_use_tls if user.role in {"admin", "superadmin"} else settings.smtp_use_tls,
        resend_api_key=(resend_api_key.strip() or settings.resend_api_key) if user.role in {"admin", "superadmin"} else settings.resend_api_key,
        resend_from=(resend_from.strip() or settings.resend_from) if user.role in {"admin", "superadmin"} else settings.resend_from,
        companies_house_api_key=(companies_house_api_key.strip() or settings.companies_house_api_key) if user.role in {"admin", "superadmin"} else settings.companies_house_api_key,
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
    request: Request,
    base_url: str = Form("http://127.0.0.1:31415"),
    api_key: str = Form(""),
    llm_model: str = Form("local-model"),
):
    if not api_key.strip():
        with SessionLocal() as db:
            workspace, _ = current_workspace(request, db)
            config = db.scalar(select(AIProviderConfig).where(AIProviderConfig.workspace_id == workspace.id))
            if config:
                api_key = decrypt_secret(config.encrypted_api_key)
                base_url = config.base_url
                llm_model = config.model_name
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


@app.post("/settings/email-account/disconnect")
def disconnect_email_account(request: Request):
    with SessionLocal() as db:
        workspace, _ = current_workspace(request, db)
        account = db.scalar(select(EmailAccount).where(EmailAccount.workspace_id == workspace.id, EmailAccount.status != "disconnected").order_by(EmailAccount.id))
        if account:
            account.status = "disconnected"
            db.commit()
    return RedirectResponse("/?tab=settings", status_code=303)


@app.post("/settings/blocklist")
def add_workspace_blocklist(request: Request, domain: str = Form()):
    normalized = domain.strip().lower().lstrip("@")
    if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", normalized):
        raise HTTPException(400, "Enter a valid company domain.")
    with SessionLocal() as db:
        workspace, _ = current_workspace(request, db)
        db.add(Suppression(workspace_id=workspace.id, domain=normalized, reason="workspace_blocklist"))
        db.commit()
    return RedirectResponse("/?tab=settings", status_code=303)


@app.get("/settings/gmail/connect")
def gmail_connect(request: Request):
    if not settings.google_client_id or not settings.google_redirect_uri:
        raise HTTPException(503, "Google OAuth is not configured.")
    state = secrets.token_urlsafe(32)
    request.session["gmail_oauth_state"] = state
    params = urlencode({"client_id": settings.google_client_id, "redirect_uri": settings.google_redirect_uri, "response_type": "code", "access_type": "offline", "prompt": "consent", "scope": "https://www.googleapis.com/auth/gmail.send https://www.googleapis.com/auth/userinfo.email", "state": state})
    return RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{params}")


@app.get("/settings/gmail/callback")
def gmail_callback(request: Request, code: str = "", state: str = ""):
    if not code or not state or not secrets.compare_digest(state, request.session.pop("gmail_oauth_state", "")):
        raise HTTPException(400, "Invalid Gmail authorization state.")
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(503, "Google OAuth is not configured.")
    token_response = httpx.post("https://oauth2.googleapis.com/token", data={"code": code, "client_id": settings.google_client_id, "client_secret": settings.google_client_secret, "redirect_uri": settings.google_redirect_uri, "grant_type": "authorization_code"}, timeout=30)
    token_response.raise_for_status()
    token = token_response.json()
    profile = httpx.get("https://www.googleapis.com/oauth2/v2/userinfo", headers={"Authorization": f"Bearer {token['access_token']}"}, timeout=30)
    profile.raise_for_status()
    email = profile.json().get("email")
    if not email:
        raise HTTPException(400, "Google did not return a sender email.")
    credentials = {"access_token": token.get("access_token", ""), "refresh_token": token.get("refresh_token", ""), "expires_at": int(datetime.now(timezone.utc).timestamp()) + int(token.get("expires_in", 3600))}
    with SessionLocal() as db:
        workspace, _ = current_workspace(request, db)
        account = db.scalar(select(EmailAccount).where(EmailAccount.workspace_id == workspace.id).order_by(EmailAccount.id))
        if not account:
            account = EmailAccount(workspace_id=workspace.id, provider="gmail", email=email, encrypted_credentials=encrypt_secret(json.dumps(credentials)))
            db.add(account)
        else:
            account.provider = "gmail"
            account.email = email
            account.encrypted_credentials = encrypt_secret(json.dumps(credentials))
            account.status = "active"
        db.commit()
    return RedirectResponse("/?tab=settings", status_code=303)
