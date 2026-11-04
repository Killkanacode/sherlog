"""Who may call the API, and what they may do.

Roles, each including the ones before it:
  viewer    read incidents, postmortems, the live stream
  operator  approve, reject and run actions; drive the Chaos Lab
  admin     change settings, manage API keys, read the audit log

Machines send `Authorization: Bearer <api key>`. The web console exchanges a
key for a signed, HttpOnly, SameSite=Strict session cookie at /auth/login;
with a cookie, every write must also carry `X-Requested-With: sherlog`, which
a cross-site form or script can't add. Keys are stored only as SHA-256
hashes. On the very first start, if no key is configured, an admin key is
generated and written to the log once.
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request

from sherlog import db
from sherlog.config import settings

log = logging.getLogger("sherlog.auth")

ROLES = ("viewer", "operator", "admin")
RANK = {role: i for i, role in enumerate(ROLES)}
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
COOKIE = "sherlog_session"
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "sherlog"
LOGIN_ATTEMPTS_PER_MIN = 10


@dataclass(frozen=True)
class Principal:
    name: str
    role: str
    via: str  # "key" or "session"

    def to_dict(self) -> dict:
        return {"name": self.name, "role": self.role}


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return "slg_" + secrets.token_urlsafe(32)


class Auth:
    def __init__(self) -> None:
        self.keys: dict[str, tuple[str, str]] = {}  # sha256 -> (name, role)
        self.stored: list[dict] = []  # keys created through the API
        self.secret = b""
        self._attempts: dict[str, deque] = defaultdict(deque)

    # ------------------------------------------------------------ setup
    async def load(self) -> None:
        state = await db.get_setting("auth") or {}
        secret = settings.secret_key or state.get("secret")
        if not secret:
            secret = secrets.token_urlsafe(48)
        self.secret = secret.encode()
        self.stored = state.get("keys", [])
        self.keys = {}
        for entry in filter(None, (e.strip() for e in settings.api_keys.split(","))):
            name, role, hashed = entry.split(":", 2)
            if role not in RANK:
                raise ValueError(f"SHERLOG_API_KEYS: unknown role {role!r} for {name}")
            self.keys[hashed.lower()] = (name, role)
        for key in self.stored:
            self.keys[key["hash"]] = (key["name"], key["role"])
        if settings.admin_token:
            self.keys[digest(settings.admin_token)] = ("admin", "admin")
        if not self.keys:
            token = new_token()
            self.stored.append({"name": "admin", "role": "admin", "hash": digest(token), "created_at": time.time()})
            self.keys[digest(token)] = ("admin", "admin")
            log.warning(
                "No API key configured: created the first admin key. It is shown only this once: %s  "
                "(set SHERLOG_ADMIN_TOKEN or SHERLOG_API_KEYS to manage keys yourself)", token,
            )
        await self._save(secret if not settings.secret_key else None)

    async def _save(self, secret: str | None = None) -> None:
        state = await db.get_setting("auth") or {}
        if secret:
            state["secret"] = secret
        state["keys"] = self.stored
        await db.put_setting("auth", state)

    # ------------------------------------------------------------ keys
    def principal_for(self, token: str) -> Principal | None:
        found = self.keys.get(digest(token))
        return Principal(found[0], found[1], "key") if found else None

    async def create_key(self, name: str, role: str) -> str:
        if role not in RANK:
            raise ValueError(f"role must be one of {', '.join(ROLES)}")
        if any(key["name"] == name for key in self.stored) or any(n == name for n, _ in self.keys.values()):
            raise ValueError(f"a key named {name} already exists")
        token = new_token()
        self.stored.append({"name": name, "role": role, "hash": digest(token), "created_at": time.time()})
        self.keys[digest(token)] = (name, role)
        await self._save()
        return token

    async def revoke(self, name: str) -> bool:
        before = len(self.stored)
        self.stored = [key for key in self.stored if key["name"] != name]
        self.keys = {h: v for h, v in self.keys.items() if v[0] != name}
        await self._save()
        return len(self.stored) < before

    def list_keys(self) -> list[dict]:
        names = {}
        for name, role in self.keys.values():
            names[name] = role
        created = {key["name"]: key.get("created_at") for key in self.stored}
        return [
            {"name": n, "role": r, "source": "api" if n in created else "environment", "created_at": created.get(n)}
            for n, r in sorted(names.items())
        ]

    # ------------------------------------------------------------ sessions
    def _sign(self, payload: bytes) -> str:
        return base64.urlsafe_b64encode(hmac.new(self.secret, payload, hashlib.sha256).digest()).decode().rstrip("=")

    def session_cookie(self, principal: Principal) -> str:
        body = json.dumps({"n": principal.name, "r": principal.role, "exp": int(time.time()) + settings.session_hours * 3600})
        payload = base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")
        return f"{payload}.{self._sign(payload.encode())}"

    def principal_from_cookie(self, value: str) -> Principal | None:
        try:
            payload, signature = value.split(".", 1)
            if not hmac.compare_digest(signature, self._sign(payload.encode())):
                return None
            data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        except Exception:  # noqa: BLE001 - any malformed cookie is simply not a session
            return None
        if data["exp"] < time.time():
            return None
        if (data["n"], data["r"]) not in self.keys.values():  # key revoked or its role changed
            return None
        return Principal(data["n"], data["r"], "session")

    # ------------------------------------------------------------ requests
    def authenticate(self, request: Request) -> Principal | None:
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            return self.principal_for(header[7:].strip())
        cookie = request.cookies.get(COOKIE)
        return self.principal_from_cookie(cookie) if cookie else None

    def allow_login(self, ip: str) -> bool:
        now = time.monotonic()
        attempts = self._attempts[ip]
        while attempts and now - attempts[0] > 60:
            attempts.popleft()
        if len(attempts) >= LOGIN_ATTEMPTS_PER_MIN:
            return False
        attempts.append(now)
        return True


auth = Auth()


def client_ip(request: Request) -> str | None:
    return request.headers.get("x-real-ip") or (request.client.host if request.client else None)


def check(request: Request, role: str, method: str | None = None) -> Principal:
    principal = auth.authenticate(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="authentication required")
    request.state.principal = principal
    if RANK[principal.role] < RANK[role]:
        raise HTTPException(status_code=403, detail=f"this needs the {role} role")
    method = method or request.method
    if principal.via == "session" and method not in SAFE_METHODS and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise HTTPException(status_code=403, detail="missing X-Requested-With header")
    return principal


def require(role: str):
    async def dependency(request: Request) -> Principal:
        return check(request, role)

    return Depends(dependency)


viewer = require("viewer")
operator = require("operator")
admin = require("admin")
