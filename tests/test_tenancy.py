import pytest
from fastapi import HTTPException

from app.db import Base, SessionLocal, engine
from app.models import Candidate, User, Workspace, WorkspaceMembership
from app.tenancy import require_owned


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


def test_workspace_scope_blocks_cross_workspace_reads():
    with SessionLocal() as db:
        owner_a = User(email="a@example.com")
        owner_b = User(email="b@example.com")
        workspace_a = Workspace(name="A", slug="a", owner_user_id=None)
        workspace_b = Workspace(name="B", slug="b", owner_user_id=None)
        db.add_all([owner_a, owner_b, workspace_a, workspace_b])
        db.flush()
        workspace_a.owner_user_id = owner_a.id
        workspace_b.owner_user_id = owner_b.id
        db.add_all([
            WorkspaceMembership(workspace_id=workspace_a.id, user_id=owner_a.id),
            WorkspaceMembership(workspace_id=workspace_b.id, user_id=owner_b.id),
            Candidate(workspace_id=workspace_a.id, user_id=owner_a.id, name="A", email="a@example.com", cv_yaml="cv: {}"),
            Candidate(workspace_id=workspace_b.id, user_id=owner_b.id, name="B", email="b@example.com", cv_yaml="cv: {}"),
        ])
        db.commit()
        candidate_a = db.query(Candidate).filter_by(workspace_id=workspace_a.id).one()
        candidate_b = db.query(Candidate).filter_by(workspace_id=workspace_b.id).one()
        assert require_owned(db, Candidate, candidate_a.id, workspace_a).id == candidate_a.id
        with pytest.raises(HTTPException) as error:
            require_owned(db, Candidate, candidate_b.id, workspace_a)
        assert error.value.status_code == 404
