import uuid
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.db.models import Base, LlmCall
from app.llm import client, metrics


def _static_engine():
    # StaticPool + check_same_thread=False: one shared connection usable from
    # the asyncio.to_thread worker that record_call runs on.
    eng = create_engine("sqlite+pysqlite:///:memory:", future=True,
                        poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    return eng


def test_record_call_writes_row(monkeypatch):
    eng = _static_engine()
    monkeypatch.setattr(metrics, "SessionLocal",
                        sessionmaker(bind=eng, future=True))
    metrics.record_call(stage="classify", model="anthropic/claude-haiku-4-5",
                        user_id="u1", input_tokens=100, output_tokens=20,
                        cost_usd=0.0003, duration_ms=250, outcome="success")
    with sessionmaker(bind=eng, future=True)() as s:
        row = s.execute(select(LlmCall)).scalar_one()
        assert row.stage == "classify"
        assert row.input_tokens == 100
        assert row.outcome == "success"
        assert row.duration_ms == 250


def test_record_call_never_raises(monkeypatch):
    class _Boom:
        def __call__(self):
            raise RuntimeError("db down")
    monkeypatch.setattr(metrics, "SessionLocal", _Boom())
    metrics.record_call(stage="classify", model="m", duration_ms=1, outcome="success")
    # reaching here without an exception IS the assertion
