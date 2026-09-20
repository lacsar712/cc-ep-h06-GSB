import hashlib
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.cqrs import (
    ConflictError,
    DomainError,
    abort_run,
    attach_artifact,
    complete_run,
    list_events,
    rebuild_projection_from_events,
    record_metric,
    start_run,
)
from app.database import Base
from app.models import RunProjection


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # JSONB not available on SQLite — remap via create_all with JSON
    from sqlalchemy import JSON
    from sqlalchemy.dialects.postgresql import JSONB

    # For SQLite tests, compile JSONB as JSON
    from sqlalchemy.ext.compiler import compiles

    @compiles(JSONB, "sqlite")
    def _compile_jsonb_sqlite(_type, compiler, **kw):
        return "JSON"

    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def test_start_and_complete_happy_path(db):
    run = start_run(
        db,
        actor="researcher",
        project="p1",
        name="n1",
        dataset_content_sha256=sha("ds"),
        code_commit_sha="abc1234",
        description="d",
    )
    assert run.status == "running"
    assert run.version == 1

    run = record_metric(
        db,
        run_id=run.id,
        actor="researcher",
        name="acc",
        value=0.9,
        step=1,
        expected_version=1,
    )
    assert run.version == 2
    assert len(run.metrics_json) == 1

    run = complete_run(
        db,
        run_id=run.id,
        actor="researcher",
        result_summary="done",
        expected_version=2,
    )
    assert run.status == "completed"
    assert run.version == 3

    with pytest.raises(ConflictError):
        record_metric(
            db,
            run_id=run.id,
            actor="researcher",
            name="acc",
            value=0.95,
            step=2,
            expected_version=3,
        )


def test_optimistic_lock_conflict(db):
    run = start_run(
        db,
        actor="researcher",
        project="p1",
        name="n1",
        dataset_content_sha256=sha("ds2"),
        code_commit_sha="abc1234",
        description=None,
    )
    with pytest.raises(ConflictError):
        record_metric(
            db,
            run_id=run.id,
            actor="researcher",
            name="loss",
            value=1.0,
            step=1,
            expected_version=0,
        )


def test_abort_terminal(db):
    run = start_run(
        db,
        actor="researcher",
        project="p1",
        name="n1",
        dataset_content_sha256=sha("ds3"),
        code_commit_sha="abc1234",
        description=None,
    )
    run = abort_run(
        db,
        run_id=run.id,
        actor="researcher",
        reason="OOM",
        expected_version=1,
    )
    assert run.status == "aborted"
    with pytest.raises(ConflictError):
        complete_run(
            db,
            run_id=run.id,
            actor="researcher",
            result_summary="nope",
            expected_version=2,
        )


def test_abort_running_succeeds_with_reason(db):
    run = start_run(
        db,
        actor="researcher",
        project="p1",
        name="n1",
        dataset_content_sha256=sha("ds-abort-running"),
        code_commit_sha="abc1234",
        description=None,
    )
    run = abort_run(
        db,
        run_id=run.id,
        actor="researcher",
        reason="人工中止：超预算",
        expected_version=1,
    )
    assert run.status == "aborted"
    assert run.version == 2
    assert run.abort_reason == "人工中止：超预算"
    assert run.finished_at is not None
    events = list_events(db, run.id)
    assert [e.event_type for e in events] == ["RunStarted", "RunAborted"]
    assert events[-1].payload_json["reason"] == "人工中止：超预算"


def test_abort_after_completed_rejected_and_unchanged(db):
    run = start_run(
        db,
        actor="researcher",
        project="p1",
        name="n1",
        dataset_content_sha256=sha("ds-abort-completed"),
        code_commit_sha="abc1234",
        description=None,
    )
    run = complete_run(
        db,
        run_id=run.id,
        actor="researcher",
        result_summary="done",
        expected_version=1,
    )
    assert run.status == "completed"
    assert run.version == 2

    with pytest.raises(ConflictError):
        abort_run(
            db,
            run_id=run.id,
            actor="researcher",
            reason="终态后再中止，应被拒绝",
            expected_version=2,
        )

    stored = db.get(RunProjection, run.id)
    assert stored.status == "completed"
    assert stored.version == 2
    assert stored.abort_reason is None
    assert stored.result_summary == "done"
    events = list_events(db, run.id)
    assert [e.event_type for e in events] == ["RunStarted", "RunCompleted"]


def test_abort_after_aborted_rejected_and_unchanged(db):
    run = start_run(
        db,
        actor="researcher",
        project="p1",
        name="n1",
        dataset_content_sha256=sha("ds-abort-twice"),
        code_commit_sha="abc1234",
        description=None,
    )
    run = abort_run(
        db,
        run_id=run.id,
        actor="researcher",
        reason="第一次中止：OOM",
        expected_version=1,
    )
    assert run.status == "aborted"
    assert run.version == 2

    with pytest.raises(ConflictError):
        abort_run(
            db,
            run_id=run.id,
            actor="researcher",
            reason="第二次中止，应被拒绝",
            expected_version=2,
        )

    stored = db.get(RunProjection, run.id)
    assert stored.status == "aborted"
    assert stored.version == 2
    assert stored.abort_reason == "第一次中止：OOM"
    events = list_events(db, run.id)
    assert [e.event_type for e in events] == ["RunStarted", "RunAborted"]


def test_abort_requires_nonempty_reason():
    from pydantic import ValidationError

    from app.schemas import AbortRunCommand

    with pytest.raises(ValidationError):
        AbortRunCommand(reason="", expected_version=1)



def test_projection_matches_event_replay(db):
    run = start_run(
        db,
        actor="researcher",
        project="p1",
        name="n1",
        dataset_content_sha256=sha("ds4"),
        code_commit_sha="deadbeef",
        description="x",
        run_id=uuid4(),
    )
    run = record_metric(
        db,
        run_id=run.id,
        actor="researcher",
        name="f1",
        value=1.5,
        step=0,
        expected_version=run.version,
    )
    run = attach_artifact(
        db,
        run_id=run.id,
        actor="researcher",
        name="model.bin",
        uri="file:///tmp/model.bin",
        content_sha256=sha("model"),
        media_type="application/octet-stream",
        expected_version=run.version,
    )
    run = complete_run(
        db,
        run_id=run.id,
        actor="researcher",
        result_summary="ok",
        expected_version=run.version,
    )

    events = list_events(db, run.id)
    assert [e.event_type for e in events] == [
        "RunStarted",
        "MetricRecorded",
        "ArtifactAttached",
        "RunCompleted",
    ]

    rebuilt = rebuild_projection_from_events(db, run.id)
    stored = db.get(RunProjection, run.id)
    assert rebuilt is not None and stored is not None
    assert rebuilt.status == stored.status
    assert rebuilt.version == stored.version
    assert rebuilt.dataset_content_sha256 == stored.dataset_content_sha256
    assert rebuilt.code_commit_sha == stored.code_commit_sha
    assert len(rebuilt.metrics_json) == len(stored.metrics_json)
    assert len(rebuilt.artifacts_json) == len(stored.artifacts_json)


def test_cannot_command_before_start(db):
    missing = uuid4()
    with pytest.raises(DomainError):
        record_metric(
            db,
            run_id=missing,
            actor="researcher",
            name="x",
            value=1,
            step=0,
            expected_version=0,
        )
