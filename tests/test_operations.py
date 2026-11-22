import pytest

from app.db import Base, SessionLocal, engine
from app.models import OutreachJob, SystemSetting, User, Workspace, WorkspaceMembership
from app.ops import enqueue_job, kill_switch_enabled, mark_job, set_setting


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


def test_durable_job_lifecycle(monkeypatch):
    pushed = []

    class FakeRedis:
        def lpush(self, queue, payload):
            pushed.append((queue, payload))

    monkeypatch.setattr("app.ops.redis.Redis.from_url", lambda *_args, **_kwargs: FakeRedis())
    with SessionLocal() as db:
        user = User(email="ops@example.com")
        db.add(user)
        db.flush()
        workspace = Workspace(name="Ops", slug="ops", owner_user_id=user.id)
        db.add(workspace)
        db.flush()
        db.add(WorkspaceMembership(workspace_id=workspace.id, user_id=user.id))
        db.commit()
        user_id, workspace_id = user.id, workspace.id
    job_id = enqueue_job("daily_outreach", {"requested_at": "now"}, workspace_id, user_id)
    assert pushed and '"job_id": %d' % job_id in pushed[0][1]
    mark_job(job_id, "running", progress=40)
    with SessionLocal() as db:
        job = db.get(OutreachJob, job_id)
        assert job.status == "running"
        assert job.progress == 40
    mark_job(job_id, "succeeded", progress=100)
    with SessionLocal() as db:
        assert db.get(OutreachJob, job_id).finished_at is not None


def test_kill_switch_is_persistent():
    with SessionLocal() as db:
        set_setting(db, "outreach_kill_switch", "true")
        db.commit()
    assert kill_switch_enabled() is True
