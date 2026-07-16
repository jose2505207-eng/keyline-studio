"""Optional token authentication + organization tenancy.

Modes (AUTH_MODE env):

* ``disabled`` (default) — backward compatible single-tenant behavior:
  every request acts as the default organization's owner. Nothing changes
  for existing local/self-hosted deployments.
* ``token`` — every /api request (except the public health endpoints and
  the ADMIN_TOKEN-guarded admin routes) must present
  ``Authorization: Bearer <token>``. The actor's organization scopes every
  project/DTM lookup; cross-tenant access is a 404 (existence is not
  leaked). Viewers are read-only.

Tokens are random 256-bit secrets shown once at creation; only their
sha256 is stored. This is deliberately simple (no sessions, no password
storage) — an SSO/OIDC layer can replace token issuance later without
touching the tenancy enforcement below.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass

from . import db
from .migrations import DEFAULT_ORG_ID

log = logging.getLogger(__name__)

ROLES = ("owner", "admin", "designer", "field_operator", "viewer")
# roles allowed to mutate (create projects, start analysis, upload, cancel…)
WRITE_ROLES = {"owner", "admin", "designer", "field_operator"}

_TOKEN_PREFIX = "kls_"


@dataclass(frozen=True)
class Actor:
    user_id: str | None
    org_id: str
    role: str
    name: str | None = None

    @property
    def can_write(self) -> bool:
        return self.role in WRITE_ROLES


DEFAULT_ACTOR = Actor(user_id=None, org_id=DEFAULT_ORG_ID, role="owner",
                      name="local")


def auth_mode() -> str:
    v = os.environ.get("AUTH_MODE", "disabled").strip().lower()
    return v if v in ("disabled", "token") else "disabled"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_token(user_id: str, label: str | None = None) -> str:
    """Create and store a new API token; the raw value is returned exactly
    once and never persisted."""
    token = _TOKEN_PREFIX + secrets.token_urlsafe(32)
    db.create_api_token(user_id, hash_token(token), label)
    return token


def resolve_actor(authorization: str | None) -> Actor | None:
    """Bearer token -> Actor, or None when invalid/missing (token mode)."""
    if auth_mode() == "disabled":
        return DEFAULT_ACTOR
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization[len("Bearer "):].strip()
    if not token:
        return None
    user = db.user_for_token_hash(hash_token(token))
    if user is None:
        return None
    return Actor(user_id=user["id"], org_id=user["org_id"],
                 role=user.get("role") or "viewer", name=user.get("name"))


# ---------------------------------------------------------------------------
# Tenancy middleware

# paths that require no auth even in token mode
_PUBLIC_PATHS = re.compile(
    r"^/(docs|openapi\.json|redoc)$|^/api/(health|ready)$")
# admin routes carry their own ADMIN_TOKEN guard
_ADMIN_PATHS = re.compile(r"^/api/admin/")

_PROJECT_PATH = re.compile(r"^/api/projects/([^/]+)")
_DTM_PATH = re.compile(r"^/api/dtms/(dtm_[A-Za-z0-9]+)")

# Mutating requests that are part of the local-dev upload loop are exempt
# from the viewer read-only rule (they carry presigned-style keys).
_LOCAL_UPLOAD = re.compile(r"^/api/local-uploads/")


class TenancyError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


def check_request(method: str, path: str, authorization: str | None) -> Actor:
    """Resolve the actor and enforce org scoping + role for one request.
    Raises TenancyError with the HTTP status to return."""
    if _PUBLIC_PATHS.match(path) or _ADMIN_PATHS.match(path):
        return DEFAULT_ACTOR
    actor = resolve_actor(authorization)
    if actor is None:
        raise TenancyError(401, "Missing or invalid API token")

    if method not in ("GET", "HEAD", "OPTIONS") \
            and not _LOCAL_UPLOAD.match(path) and not actor.can_write:
        raise TenancyError(403, "Your role is read-only")

    m = _PROJECT_PATH.match(path)
    if m:
        project = db.get_project(m.group(1))
        # missing projects fall through: route handlers return their own 404
        if project is not None and \
                (project.get("org_id") or DEFAULT_ORG_ID) != actor.org_id:
            raise TenancyError(404, "Project not found")
    m = _DTM_PATH.match(path)
    if m:
        dtm = db.get_dtm(m.group(1))
        if dtm is not None and \
                (dtm.get("org_id") or DEFAULT_ORG_ID) != actor.org_id:
            raise TenancyError(404, "DTM not found")
    return actor


# ---------------------------------------------------------------------------
# Simple in-process rate limiting for expensive endpoints (single-process;
# a hosted multi-replica deployment should rate limit at the proxy too).

_EXPENSIVE = re.compile(
    r"^/api/projects/[^/]+/(analyze|reanalyze|analysis-runs/[^/]+/retry)$")
_BUCKET: dict[str, list[float]] = {}


def _rate_limit_per_minute() -> int | None:
    """Enforced in token mode (default 10/min/actor) or whenever the env var
    is set explicitly; disabled-auth local deployments are not throttled
    (a shared anonymous bucket would randomly block a single legit user)."""
    raw = os.environ.get("ANALYSIS_RATE_LIMIT_PER_MINUTE", "")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            return 10
    return 10 if auth_mode() == "token" else None


def check_rate_limit(method: str, path: str, key: str) -> None:
    if method != "POST" or not _EXPENSIVE.match(path):
        return
    limit = _rate_limit_per_minute()
    if limit is None:
        return
    now = time.time()
    window = [t for t in _BUCKET.get(key, []) if now - t < 60.0]
    if len(window) >= limit:
        raise TenancyError(
            429, "Too many analysis requests — wait a minute and retry")
    window.append(now)
    _BUCKET[key] = window
