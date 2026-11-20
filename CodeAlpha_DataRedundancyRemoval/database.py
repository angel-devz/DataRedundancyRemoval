"""Database layer (SQLAlchemy Core).

Works with SQLite locally and any cloud SQL database (PostgreSQL on AWS RDS,
Azure Database, Google Cloud SQL, Render, Neon...) via the DATABASE_URL env var.
"""

import json
import os
from datetime import datetime, timezone

from sqlalchemy import (Column, DateTime, Float, Integer, MetaData, String, Table, Text,
                        create_engine, func, insert, or_, select)
from sqlalchemy.exc import IntegrityError

import dedup

metadata = MetaData()

records = Table(
    "records", metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(120), nullable=False),
    Column("email", String(254), nullable=False, unique=True),
    Column("phone", String(15), nullable=False, unique=True),
    Column("city", String(80), nullable=False, default=""),
    Column("fingerprint", String(64), nullable=False, unique=True),
    Column("name_block", String(8), index=True),
    Column("email_block", String(8), index=True),
    Column("phone_block", String(8), index=True),
    Column("status", String(20), nullable=False),  # unique | false_positive
    Column("created_at", DateTime(timezone=True), nullable=False),
)

ingestion_log = Table(
    "ingestion_log", metadata,
    Column("id", Integer, primary_key=True),
    Column("input", Text, nullable=False),
    Column("status", String(20), nullable=False, index=True),
    Column("reason", String(200), nullable=False),
    Column("score", Float, nullable=False, default=0.0),
    Column("matched_id", Integer),
    Column("record_id", Integer),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


def _now():
    return datetime.now(timezone.utc)


class Store:
    def __init__(self, url: str | None = None):
        url = url or os.environ.get("DATABASE_URL", "sqlite:///data.db")
        # Cloud hosts hand out postgres:// or postgresql:// URLs; use the psycopg 3 driver.
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                url = "postgresql+psycopg://" + url[len(prefix):]
        self.engine = create_engine(url, pool_pre_ping=True)
        metadata.create_all(self.engine)

    # ----- lookups used by dedup.classify -----
    def _one(self, query):
        with self.engine.connect() as conn:
            row = conn.execute(query).mappings().first()
            return dict(row) if row else None

    def find_by_fingerprint(self, fp):
        return self._one(select(records).where(records.c.fingerprint == fp))

    def find_by_email(self, email):
        return self._one(select(records).where(records.c.email == email))

    def find_by_phone(self, phone):
        return self._one(select(records).where(records.c.phone == phone))

    def candidates(self, blocks: dict):
        q = select(records).where(or_(
            records.c.name_block == blocks["name_block"],
            records.c.email_block == blocks["email_block"],
            records.c.phone_block == blocks["phone_block"],
        )).limit(200)
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(q).mappings()]

    # ----- ingestion -----
    def ingest(self, raw: dict, dry_run: bool = False) -> dedup.Decision:
        """Validate + classify a record, append it only if unique, and log the decision."""
        decision = dedup.classify(raw, self)
        if dry_run:
            return decision

        record_id = None
        with self.engine.begin() as conn:
            if decision.should_insert:
                r = decision.record
                try:
                    with conn.begin_nested():
                        result = conn.execute(insert(records).values(
                            name=r.name, email=r.email, phone=r.phone, city=r.city,
                            fingerprint=r.fingerprint, status=decision.status,
                            created_at=_now(), **r.blocks,
                        ))
                    record_id = result.inserted_primary_key[0]
                except IntegrityError:
                    # Another request inserted the same data a moment ago.
                    decision = dedup.Decision(dedup.REDUNDANT, "duplicate blocked by database constraint",
                                              1.0, None, r)
            conn.execute(insert(ingestion_log).values(
                input=json.dumps(raw, default=str)[:2000], status=decision.status,
                reason=decision.reason[:200], score=round(decision.score, 3),
                matched_id=decision.matched_id, record_id=record_id, created_at=_now(),
            ))
        decision.record_id = record_id
        return decision

    # ----- reporting -----
    def list_records(self, limit=100):
        q = select(records.c.id, records.c.name, records.c.email, records.c.phone,
                   records.c.city, records.c.status, records.c.created_at
                   ).order_by(records.c.id.desc()).limit(limit)
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(q).mappings()]

    def recent_log(self, limit=50):
        q = select(ingestion_log).order_by(ingestion_log.c.id.desc()).limit(limit)
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(q).mappings()]

    def stats(self):
        with self.engine.connect() as conn:
            counts = dict(conn.execute(
                select(ingestion_log.c.status, func.count()).group_by(ingestion_log.c.status)).all())
            stored = conn.execute(select(func.count()).select_from(records)).scalar_one()
        received = sum(counts.values())
        blocked = counts.get(dedup.REDUNDANT, 0)
        return {
            "received": received,
            "stored": stored,
            "unique": counts.get(dedup.UNIQUE, 0),
            "false_positive": counts.get(dedup.FALSE_POSITIVE, 0),
            "redundant": blocked,
            "invalid": counts.get(dedup.INVALID, 0),
            "redundancy_rate": round(blocked / received, 3) if received else 0,
        }
