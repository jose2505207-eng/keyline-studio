"""Health/readiness endpoints, request ids, and the central error boundary."""


def test_health_is_public_and_dependency_free(drone_env, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "token")  # still public
    r = drone_env.client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_ready_reports_dependencies(drone_env):
    r = drone_env.client.get("/api/ready")
    body = r.json()
    # DB + local dtm dir work in the test env; Redis points at a closed
    # port, so auto mode is degraded-but-ready (inline fallback exists)
    assert r.status_code == 200, body
    assert body["database"]["ok"] is True
    assert body["queue"]["ok"] is False
    assert body["status"] == "degraded"
    assert body["analysis_execution"] == "auto"


def test_ready_503_when_rq_mode_and_queue_down(drone_env, monkeypatch):
    monkeypatch.setenv("ANALYSIS_EXECUTION", "rq")
    r = drone_env.client.get("/api/ready")
    assert r.status_code == 503
    assert "queue" in r.json()["hard_failures"]


def test_responses_carry_request_id_and_security_headers(drone_env):
    r = drone_env.client.get("/api/health")
    assert r.headers.get("X-Request-ID")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    # a client-supplied id is propagated for correlation
    r = drone_env.client.get("/api/health",
                             headers={"X-Request-ID": "corr-123"})
    assert r.headers["X-Request-ID"] == "corr-123"


def test_unhandled_errors_hide_internals(drone_env, monkeypatch):
    import app.db as db

    def _boom(pid):
        raise RuntimeError("secret internal path /etc/passwd")

    monkeypatch.setattr(db, "get_project", _boom)
    r = drone_env.client.get("/api/projects/whatever")
    assert r.status_code == 500
    body = r.json()
    assert body["detail"] == "Internal server error"
    assert "request_id" in body
    assert "secret" not in r.text and "passwd" not in r.text
