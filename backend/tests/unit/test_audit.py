"""统一审计日志格式。"""
from __future__ import annotations

import logging

from app.core import trace
from app.core.audit import audit


def test_audit_event_shape(caplog):
    with caplog.at_level(logging.INFO, logger="app.audit"):
        with trace.begin_trace():
            audit("user.create", stage="audit.user",
                  actor_id="admin-1", actor_role="admin",
                  target_id="user-1", username="alice", role="student")

    rec = [r for r in caplog.records if r.name == "app.audit"][-1]
    assert rec.event == "audit.user.create"
    assert rec.stage == "audit.user"
    assert rec.layer == "audit"
    assert rec.ok is True
    assert rec.actor_id == "admin-1"
    assert rec.target_id == "user-1"
    assert len(rec.trace_id) == 32


def test_audit_failure_is_warning_and_not_ok(caplog):
    with caplog.at_level(logging.WARNING, logger="app.audit"):
        audit("auth.login", stage="audit.auth", ok=False,
              level=logging.WARNING, reason="invalid_credentials",
              username="alice")

    rec = [r for r in caplog.records if r.name == "app.audit"][-1]
    assert rec.levelno == logging.WARNING
    assert rec.event == "audit.auth.login"
    assert rec.ok is False
    assert rec.reason == "invalid_credentials"
