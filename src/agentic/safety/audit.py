"""Tamper-evident immutable audit trail for HITL and data-lifecycle events."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.agentic.core.config import Settings, get_settings, is_strict_env
from src.agentic.core.context import get_request_id
from src.agentic.observability.metrics import OBSERVABILITY_ERRORS_TOTAL

logger = logging.getLogger(__name__)
_AUDIT_LOGGER = logging.getLogger("agentic.audit.immutable")
_SQLITE_LOCK = threading.Lock()


def compute_audit_integrity_hash(
    *,
    event_id: str,
    event_type: str,
    actor_id: str,
    thread_id: str | None,
    step_id: str | None,
    decision: str | None,
    details: dict[str, Any],
    created_at: str,
    signing_key: str,
    trace_id: str | None = None,
    risk_level: str | None = None,
) -> str:
    """Compute an HMAC-SHA256 digest over canonical audit event fields."""
    if details.get("_integrity_version") == 2:
        if trace_id is None:
            raw_trace = details.get("_integrity_trace_id")
            trace_id = str(raw_trace) if raw_trace is not None else None
        if risk_level is None:
            raw_risk = details.get("_integrity_risk_level")
            risk_level = str(raw_risk) if raw_risk is not None else None
    canonical_fields: dict[str, Any] = {
        "id": event_id,
        "event_type": event_type,
        "actor_id": actor_id,
        "thread_id": thread_id,
        "step_id": step_id,
        "decision": decision,
        "details": details,
        "created_at": created_at,
    }
    # Version 2 binds correlation and risk metadata as security-relevant fields.
    # Preserve verification compatibility for pre-v2 records that did not sign
    # these columns.
    if trace_id is not None or risk_level is not None or details.get("_integrity_version") == 2:
        canonical_fields["trace_id"] = trace_id
        canonical_fields["risk_level"] = risk_level
    canonical = json.dumps(
        canonical_fields,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hmac.new(
        signing_key.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _write_postgres_audit(
    *,
    settings: Settings,
    event_id: str,
    event_type: str,
    actor_id: str,
    thread_id: str | None,
    step_id: str | None,
    trace_id: str | None,
    decision: str | None,
    risk_level: str | None,
    details_json: str,
    integrity_hash: str,
    created_at: str,
) -> None:
    import psycopg

    with psycopg.connect(
        settings.postgres_uri, prepare_threshold=None, connect_timeout=3
    ) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('audit_logs_chain'))")
            cur.execute(
                "SELECT integrity_hash FROM audit_logs ORDER BY chain_seq DESC, created_at DESC LIMIT 1"
            )
            row = cur.fetchone()
            prev_hash = str(row[0]) if row and row[0] else "GENESIS"
            chained_hash = hmac.new(
                settings.checkpoint_signing_key.encode("utf-8"),
                f"{prev_hash}:{integrity_hash}".encode(),
                hashlib.sha256,
            ).hexdigest()
            details_obj = json.loads(details_json)
            details_obj["prev_hash"] = prev_hash
            cur.execute(
                """
                INSERT INTO audit_logs (
                    id, event_type, actor_id, thread_id, step_id, trace_id,
                    decision, risk_level, details, integrity_hash, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                """,
                (
                    event_id,
                    event_type,
                    actor_id,
                    thread_id,
                    step_id,
                    trace_id,
                    decision,
                    risk_level,
                    json.dumps(details_obj, ensure_ascii=False, sort_keys=True, default=str),
                    chained_hash,
                    created_at,
                ),
            )
        conn.commit()


def _write_sqlite_audit(
    *,
    db_path: Path,
    event_id: str,
    event_type: str,
    actor_id: str,
    thread_id: str | None,
    step_id: str | None,
    trace_id: str | None,
    decision: str | None,
    risk_level: str | None,
    details: dict[str, Any],
    signing_key: str,
    created_at: str,
) -> str:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _SQLITE_LOCK:
        conn = sqlite3.connect(str(db_path), timeout=10.0, check_same_thread=False)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    thread_id TEXT,
                    step_id TEXT,
                    trace_id TEXT,
                    decision TEXT,
                    risk_level TEXT,
                    details TEXT NOT NULL,
                    integrity_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            row = conn.execute(
                "SELECT integrity_hash FROM audit_logs ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            prev_hash = str(row[0]) if row and row[0] else "GENESIS"
            chained_details = {**details, "prev_hash": prev_hash}
            integrity_hash = compute_audit_integrity_hash(
                event_id=event_id,
                event_type=event_type,
                actor_id=actor_id,
                thread_id=thread_id,
                step_id=step_id,
                decision=decision,
                trace_id=trace_id,
                risk_level=risk_level,
                details=chained_details,
                created_at=created_at,
                signing_key=signing_key,
            )
            details_json = json.dumps(
                chained_details, ensure_ascii=False, sort_keys=True, default=str
            )
            conn.execute(
                """
                INSERT INTO audit_logs (
                    id, event_type, actor_id, thread_id, step_id, trace_id,
                    decision, risk_level, details, integrity_hash, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    event_type,
                    actor_id,
                    thread_id,
                    step_id,
                    trace_id,
                    decision,
                    risk_level,
                    details_json,
                    integrity_hash,
                    created_at,
                ),
            )
            conn.commit()
            return integrity_hash
        finally:
            conn.close()


def verify_audit_chain(
    *,
    settings: Settings | None = None,
    sqlite_path: Path | None = None,
) -> tuple[bool, int, list[str]]:
    """Verify hash-chain continuity and HMAC integrity across stored audit records.

    Supports key rotation via ``settings.checkpoint_signing_keys``. Returns
    ``(is_valid, checked_count, errors)``.
    """
    cfg = settings or get_settings()
    keys = getattr(cfg, "checkpoint_signing_keys", (cfg.checkpoint_signing_key,))
    errors: list[str] = []

    if sqlite_path is not None:
        if not sqlite_path.exists():
            return True, 0, []
        with _SQLITE_LOCK:
            conn = sqlite3.connect(str(sqlite_path), timeout=10.0, check_same_thread=False)
            try:
                rows = conn.execute(
                    "SELECT id, event_type, actor_id, thread_id, step_id, trace_id, decision, risk_level, details, "
                    "integrity_hash, created_at FROM audit_logs ORDER BY rowid ASC"
                ).fetchall()
            finally:
                conn.close()

        expected_prev = "GENESIS"
        for idx, row in enumerate(rows):
            eid, etype, actor, tid, sid, trace_id, dec, risk_level, raw_details, stored_hash, created = row
            details_obj = json.loads(raw_details) if isinstance(raw_details, str) else dict(raw_details or {})
            row_prev = details_obj.get("prev_hash")
            if row_prev != expected_prev:
                errors.append(
                    f"row[{idx}] id={eid}: prev_hash mismatch (expected={expected_prev}, got={row_prev})"
                )
            matched = False
            for k in keys:
                candidate = compute_audit_integrity_hash(
                    event_id=str(eid),
                    event_type=str(etype),
                    actor_id=str(actor),
                    thread_id=tid,
                    step_id=sid,
                    trace_id=trace_id,
                    decision=dec,
                    risk_level=risk_level,
                    details=details_obj,
                    created_at=str(created),
                    signing_key=str(k),
                )
                if hmac.compare_digest(candidate, str(stored_hash)):
                    matched = True
                    break
            if not matched:
                errors.append(f"row[{idx}] id={eid}: HMAC integrity verification failed")
            expected_prev = str(stored_hash)
        return len(errors) == 0, len(rows), errors

    import psycopg

    with (
        psycopg.connect(
            cfg.postgres_uri, prepare_threshold=None, connect_timeout=3
        ) as pg_conn,
        pg_conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT id, event_type, actor_id, thread_id, step_id, trace_id, decision, risk_level, details, "
            "integrity_hash, created_at FROM audit_logs ORDER BY chain_seq ASC, created_at ASC"
        )
        pg_rows = cur.fetchall()

    expected_prev = "GENESIS"
    for idx, row in enumerate(pg_rows):
        eid, etype, actor, tid, sid, trace_id, dec, risk_level, raw_details, stored_hash, created = row
        if isinstance(created, datetime):
            normalized = created if created.tzinfo is not None else created.replace(tzinfo=UTC)
            created_iso = normalized.astimezone(UTC).isoformat()
        else:
            created_iso = str(created)
        details_obj = json.loads(raw_details) if isinstance(raw_details, str) else dict(raw_details or {})
        row_prev = details_obj.get("prev_hash")
        if row_prev != expected_prev:
            errors.append(
                f"row[{idx}] id={eid}: prev_hash mismatch (expected={expected_prev}, got={row_prev})"
            )
        base_details = {k: v for k, v in details_obj.items() if k != "prev_hash"}
        matched = False
        for k in keys:
            event_hash = compute_audit_integrity_hash(
                event_id=str(eid),
                event_type=str(etype),
                actor_id=str(actor),
                thread_id=tid,
                step_id=sid,
                trace_id=trace_id,
                decision=dec,
                risk_level=risk_level,
                details=base_details,
                created_at=created_iso,
                signing_key=str(k),
            )
            chained = hmac.new(
                str(k).encode("utf-8"),
                f"{row_prev}:{event_hash}".encode(),
                hashlib.sha256,
            ).hexdigest()
            if hmac.compare_digest(chained, str(stored_hash)):
                matched = True
                break
        if not matched:
            errors.append(f"row[{idx}] id={eid}: chained HMAC verification failed")
        expected_prev = str(stored_hash)
    return len(errors) == 0, len(pg_rows), errors


def record_audit_event(
    *,
    event_type: str,
    actor_id: str,
    thread_id: str | None = None,
    step_id: str | None = None,
    trace_id: str | None = None,
    decision: str | None = None,
    risk_level: str | None = None,
    details: dict[str, Any] | None = None,
    settings: Settings | None = None,
    sqlite_path: Path | None = None,
) -> str:
    """Persist an append-only, HMAC-signed audit record and emit structured log.

    In PostgreSQL production deployments the record is written to ``audit_logs``
    (managed by Alembic revision ``003_audit_logs``). Outside PostgreSQL (local
    dev/test) it persists to an isolated SQLite WAL database when ``sqlite_path``
    is supplied, avoiding tracked repository files.
    """
    cfg = settings or get_settings()
    event_id = str(uuid.uuid4())
    created_at = datetime.now(UTC).isoformat()
    resolved_trace = trace_id or get_request_id()
    safe_details = {
        **dict(details or {}),
        "_integrity_version": 2,
        "_integrity_trace_id": resolved_trace,
        "_integrity_risk_level": risk_level,
    }
    integrity_hash = compute_audit_integrity_hash(
        event_id=event_id,
        event_type=event_type,
        actor_id=actor_id,
        thread_id=thread_id,
        step_id=step_id,
        trace_id=resolved_trace,
        decision=decision,
        risk_level=risk_level,
        details=safe_details,
        created_at=created_at,
        signing_key=cfg.checkpoint_signing_key,
    )
    details_json = json.dumps(safe_details, ensure_ascii=False, sort_keys=True, default=str)

    _AUDIT_LOGGER.info(
        "AUDIT_EVENT id=%s type=%s actor=%s thread_id=%s step_id=%s decision=%s risk=%s trace_id=%s hash=%s",
        event_id,
        event_type,
        actor_id,
        thread_id,
        step_id,
        decision,
        risk_level,
        resolved_trace,
        integrity_hash[:16],
    )

    if cfg.use_postgres_checkpoint or is_strict_env(cfg):
        try:
            _write_postgres_audit(
                settings=cfg,
                event_id=event_id,
                event_type=event_type,
                actor_id=actor_id,
                thread_id=thread_id,
                step_id=step_id,
                trace_id=resolved_trace,
                decision=decision,
                risk_level=risk_level,
                details_json=details_json,
                integrity_hash=integrity_hash,
                created_at=created_at,
            )
            return event_id
        except Exception as exc:
            OBSERVABILITY_ERRORS_TOTAL.labels(component="audit_log").inc()
            logger.error("Failed to persist PostgreSQL audit record '%s': %s", event_id, exc)
            if is_strict_env(cfg):
                raise RuntimeError("Audit trail persistence failed in production.") from exc

    if sqlite_path is not None:
        try:
            _write_sqlite_audit(
                db_path=sqlite_path,
                event_id=event_id,
                event_type=event_type,
                actor_id=actor_id,
                thread_id=thread_id,
                step_id=step_id,
                trace_id=resolved_trace,
                decision=decision,
                risk_level=risk_level,
                details=safe_details,
                signing_key=cfg.checkpoint_signing_key,
                created_at=created_at,
            )
        except Exception as exc:
            OBSERVABILITY_ERRORS_TOTAL.labels(component="audit_log").inc()
            logger.warning("Failed to persist local SQLite audit record '%s': %s", event_id, exc)
    return event_id
