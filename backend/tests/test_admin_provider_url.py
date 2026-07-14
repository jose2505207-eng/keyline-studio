"""Runtime provider-URL override: token guard, NodeODM verification gate,
and config precedence."""

from app import config
from app.photogrammetry.models import ProviderHealth
from app.photogrammetry.nodeodm import NodeOdmProvider


def test_requires_token(drone_env, monkeypatch):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    r = drone_env.client.post("/api/admin/provider-url",
                              json={"url": "https://x.trycloudflare.com"})
    assert r.status_code == 403  # no token configured -> always refused

    monkeypatch.setenv("ADMIN_TOKEN", "sekrit")
    r = drone_env.client.post("/api/admin/provider-url",
                              json={"url": "https://x.trycloudflare.com"},
                              headers={"X-Admin-Token": "wrong"})
    assert r.status_code == 403


def test_rejects_non_origin_urls(drone_env, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "sekrit")
    for bad in ("ftp://x", "https://x/path?query=1", "not-a-url"):
        r = drone_env.client.post("/api/admin/provider-url",
                                  json={"url": bad},
                                  headers={"X-Admin-Token": "sekrit"})
        assert r.status_code == 422, bad


def test_unreachable_node_is_not_persisted(drone_env, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "sekrit")
    monkeypatch.setenv("NODEODM_URL", "http://env-default:3000")
    r = drone_env.client.post(
        "/api/admin/provider-url",
        json={"url": "http://127.0.0.1:1"},  # nothing listens here
        headers={"X-Admin-Token": "sekrit"})
    assert r.status_code == 422
    assert config.nodeodm_url() == "http://env-default:3000"


def test_verified_url_is_applied_and_wins(drone_env, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "sekrit")
    monkeypatch.setenv("NODEODM_URL", "http://env-default:3000")
    monkeypatch.setattr(
        NodeOdmProvider, "health",
        lambda self: ProviderHealth(ok=True, provider="nodeodm",
                                    version="2.2.4", engine="odm"))
    r = drone_env.client.post(
        "/api/admin/provider-url",
        json={"url": "https://new-tunnel.trycloudflare.com"},
        headers={"X-Admin-Token": "sekrit"})
    assert r.status_code == 200
    assert r.json()["url"] == "https://new-tunnel.trycloudflare.com"
    # the override now wins over the env var (read at call time)
    assert config.nodeodm_url() == "https://new-tunnel.trycloudflare.com"
