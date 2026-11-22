"""Workspace context and ownership helpers used by every authenticated route."""

from fastapi import HTTPException, Request
from sqlalchemy import select, update

from .config import settings
from .models import AuditEvent, Campaign, Candidate, User, Workspace, WorkspaceMembership


def _legacy_email() -> str:
    return (settings.admin_username or "owner").strip().lower()


def ensure_default_workspace(db) -> tuple[Workspace, User]:
    """Return the migrated single-user workspace, creating it for fresh installs."""
    user = db.scalar(select(User).where(User.email == _legacy_email()))
    if not user:
        user = User(email=_legacy_email(), role="admin", email_verified_at=None)
        db.add(user)
        db.flush()
    workspace = db.scalar(select(Workspace).where(Workspace.slug == "owner"))
    if not workspace:
        workspace = Workspace(name="Owner workspace", slug="owner", owner_user_id=user.id)
        db.add(workspace)
        db.flush()
    if not db.scalar(
        select(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == workspace.id,
            WorkspaceMembership.user_id == user.id,
        )
    ):
        db.add(WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="owner"))
    return workspace, user


def backfill_legacy_rows(db) -> tuple[Workspace, User]:
    """Assign pre-tenancy records to the migrated owner workspace exactly once."""
    workspace, user = ensure_default_workspace(db)
    db.execute(update(Candidate).where(Candidate.workspace_id.is_(None)).values(workspace_id=workspace.id, user_id=user.id))
    db.execute(update(Campaign).where(Campaign.workspace_id.is_(None)).values(workspace_id=workspace.id, user_id=user.id))
    db.execute(update(AuditEvent).where(AuditEvent.workspace_id.is_(None)).values(workspace_id=workspace.id, user_id=user.id))
    db.commit()
    return workspace, user


def current_workspace(request: Request, db) -> tuple[Workspace, User]:
    workspace_id = request.session.get("workspace_id")
    user_id = request.session.get("user_id")
    if workspace_id and user_id:
        workspace = db.get(Workspace, workspace_id)
        user = db.get(User, user_id)
        if workspace and user and not workspace.deleted_at and user.is_active:
            membership = db.scalar(
                select(WorkspaceMembership).where(
                    WorkspaceMembership.workspace_id == workspace.id,
                    WorkspaceMembership.user_id == user.id,
                )
            )
            if membership:
                return workspace, user
        raise HTTPException(403, "Workspace access is no longer available.")
    workspace, user = ensure_default_workspace(db)
    return workspace, user


def workspace_filter(model, workspace: Workspace):
    """Central predicate for all tenant-owned queries."""
    return model.workspace_id == workspace.id


def require_owned(db, model, object_id: int, workspace: Workspace):
    """Fetch an object only when it belongs to the active workspace."""
    from sqlalchemy import select

    item = db.scalar(select(model).where(model.id == object_id, workspace_filter(model, workspace)))
    # Compatibility bridge for records created before the tenancy migration;
    # startup backfills these, but direct CLI/test calls may reach this first.
    if not item:
        legacy_item = db.scalar(select(model).where(model.id == object_id, model.workspace_id.is_(None)))
        if legacy_item:
            legacy_item.workspace_id = workspace.id
            db.flush()
            item = legacy_item
    if not item or getattr(item, "deleted_at", None) is not None:
        raise HTTPException(404, f"{model.__name__} not found")
    return item
