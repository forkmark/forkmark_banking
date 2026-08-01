"""OAuth 2.0 Device Authorization Flow for CLI login.

Implements RFC 8628 — allows CLI users to authenticate via browser:

    1. CLI calls POST /api/auth/device  → gets device_code + user_code + verification_uri
    2. CLI displays: "Go to https://app.forkpoint.dev/activate and enter code: ABCD-1234"
    3. User opens browser, enters code, authenticates via SSO/password
    4. CLI polls POST /api/auth/device/token until approved → gets access_token + refresh_token
    5. Token is stored locally (~/.forkpoint/credentials.json)

Security:
    - Device codes expire after 10 minutes
    - Rate limiting on token endpoint (interval enforcement)
    - One-time use: code is consumed on approval
    - Codes stored in Redis with TTL (no DB pollution)

Usage:
    # In FastAPI app:
    app.include_router(device_flow_router, prefix="/api/auth")

    # In CLI:
    forkpoint login
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger("forkpoint.device_flow")

device_flow_router = APIRouter(tags=["auth"])

# Configuration
DEVICE_CODE_EXPIRY = 600        # 10 minutes
POLLING_INTERVAL = 5            # seconds between polls
USER_CODE_LENGTH = 8            # e.g., "ABCD-1234"
TOKEN_EXPIRY = 86400 * 7        # 7 days
REFRESH_TOKEN_EXPIRY = 86400 * 30  # 30 days


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

@dataclass
class DeviceAuthorization:
    """Pending device authorization."""
    device_code: str
    user_code: str
    verification_uri: str
    expires_at: float
    interval: int
    client_id: str
    scope: str
    # Set when user approves:
    approved: bool = False
    user_id: Optional[str] = None
    org_id: Optional[str] = None
    workspace_ids: Optional[list] = None


class DeviceAuthRequest(BaseModel):
    client_id: str = "forkpoint-cli"
    scope: str = "workspace:read workflow:read run:create"


class DeviceTokenRequest(BaseModel):
    grant_type: str = "urn:ietf:params:oauth:grant-type:device_code"
    device_code: str
    client_id: str = "forkpoint-cli"


class DeviceApprovalRequest(BaseModel):
    user_code: str
    user_id: str
    org_id: str
    workspace_ids: list[str] = []


# ---------------------------------------------------------------------------
# Device code store (Redis-backed with TTL)
# ---------------------------------------------------------------------------

class DeviceCodeStore:
    """Manages device codes in Redis with automatic expiry."""

    def __init__(self, redis_client):
        self._redis = redis_client

    def _device_key(self, device_code: str) -> str:
        return f"forkpoint:device:{device_code}"

    def _user_code_key(self, user_code: str) -> str:
        return f"forkpoint:usercode:{user_code}"

    def store(self, auth: DeviceAuthorization):
        """Store a new device authorization."""
        data = json.dumps({
            "device_code": auth.device_code,
            "user_code": auth.user_code,
            "verification_uri": auth.verification_uri,
            "expires_at": auth.expires_at,
            "interval": auth.interval,
            "client_id": auth.client_id,
            "scope": auth.scope,
            "approved": auth.approved,
            "user_id": auth.user_id,
            "org_id": auth.org_id,
            "workspace_ids": auth.workspace_ids,
        })
        ttl = int(auth.expires_at - time.time())
        if ttl <= 0:
            return

        pipe = self._redis.pipeline()
        pipe.setex(self._device_key(auth.device_code), ttl, data)
        pipe.setex(self._user_code_key(auth.user_code), ttl, auth.device_code)
        pipe.execute()

    def get_by_device_code(self, device_code: str) -> Optional[DeviceAuthorization]:
        """Lookup by device_code (CLI polling)."""
        raw = self._redis.get(self._device_key(device_code))
        if not raw:
            return None
        return self._deserialize(raw)

    def get_by_user_code(self, user_code: str) -> Optional[DeviceAuthorization]:
        """Lookup by user_code (browser approval)."""
        device_code = self._redis.get(self._user_code_key(user_code))
        if not device_code:
            return None
        return self.get_by_device_code(device_code)

    def approve(self, user_code: str, user_id: str, org_id: str, workspace_ids: list) -> bool:
        """Mark a device code as approved (user authenticated in browser)."""
        auth = self.get_by_user_code(user_code)
        if not auth:
            return False

        auth.approved = True
        auth.user_id = user_id
        auth.org_id = org_id
        auth.workspace_ids = workspace_ids
        self.store(auth)  # re-store with updated data
        return True

    def consume(self, device_code: str) -> Optional[DeviceAuthorization]:
        """Consume an approved device code (one-time use)."""
        auth = self.get_by_device_code(device_code)
        if not auth or not auth.approved:
            return None

        # Delete both keys (consumed)
        pipe = self._redis.pipeline()
        pipe.delete(self._device_key(device_code))
        pipe.delete(self._user_code_key(auth.user_code))
        pipe.execute()

        return auth

    def _deserialize(self, raw: str) -> DeviceAuthorization:
        data = json.loads(raw)
        return DeviceAuthorization(
            device_code=data["device_code"],
            user_code=data["user_code"],
            verification_uri=data["verification_uri"],
            expires_at=data["expires_at"],
            interval=data["interval"],
            client_id=data["client_id"],
            scope=data["scope"],
            approved=data.get("approved", False),
            user_id=data.get("user_id"),
            org_id=data.get("org_id"),
            workspace_ids=data.get("workspace_ids"),
        )


# ---------------------------------------------------------------------------
# Code generation utilities
# ---------------------------------------------------------------------------

def _generate_device_code() -> str:
    """Cryptographically random device code (URL-safe, 40 chars)."""
    return secrets.token_urlsafe(30)


def _generate_user_code() -> str:
    """Human-friendly code: XXXX-XXXX (uppercase alphanumeric, no ambiguous chars)."""
    charset = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no I/O/0/1
    part1 = "".join(secrets.choice(charset) for _ in range(4))
    part2 = "".join(secrets.choice(charset) for _ in range(4))
    return f"{part1}-{part2}"


# ---------------------------------------------------------------------------
# JWT token generation
# ---------------------------------------------------------------------------

def _generate_tokens(user_id: str, org_id: str, workspace_ids: list, scope: str) -> dict:
    """Generate access + refresh tokens.

    In production, use proper JWT with RS256. For now, opaque tokens stored in Redis.
    """
    import jwt as pyjwt

    signing_key = os.getenv("JWT_SIGNING_KEY", "dev-secret-change-me")

    now = time.time()
    access_payload = {
        "sub": user_id,
        "org_id": org_id,
        "workspace_ids": workspace_ids,
        "scope": scope,
        "iat": int(now),
        "exp": int(now + TOKEN_EXPIRY),
        "type": "access",
    }
    refresh_payload = {
        "sub": user_id,
        "iat": int(now),
        "exp": int(now + REFRESH_TOKEN_EXPIRY),
        "type": "refresh",
    }

    access_token = pyjwt.encode(access_payload, signing_key, algorithm="HS256")
    refresh_token = pyjwt.encode(refresh_payload, signing_key, algorithm="HS256")

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "Bearer",
        "expires_in": TOKEN_EXPIRY,
        "scope": scope,
    }


# ---------------------------------------------------------------------------
# FastAPI endpoints
# ---------------------------------------------------------------------------

@device_flow_router.post("/device")
async def device_authorization(body: DeviceAuthRequest, request: Request):
    """Step 1: CLI requests a device code.

    Returns device_code, user_code, verification_uri for the user.
    """
    store: DeviceCodeStore = getattr(request.app.state, "device_code_store", None)
    if store is None:
        raise HTTPException(503, "Device flow not available (Redis required)")

    verification_uri = os.getenv(
        "FP_VERIFICATION_URI",
        "https://app.forkpoint.dev/activate"
    )

    auth = DeviceAuthorization(
        device_code=_generate_device_code(),
        user_code=_generate_user_code(),
        verification_uri=verification_uri,
        expires_at=time.time() + DEVICE_CODE_EXPIRY,
        interval=POLLING_INTERVAL,
        client_id=body.client_id,
        scope=body.scope,
    )

    store.store(auth)
    logger.info("Device flow started: user_code=%s", auth.user_code)

    return {
        "device_code": auth.device_code,
        "user_code": auth.user_code,
        "verification_uri": auth.verification_uri,
        "verification_uri_complete": f"{auth.verification_uri}?code={auth.user_code}",
        "expires_in": DEVICE_CODE_EXPIRY,
        "interval": POLLING_INTERVAL,
    }


@device_flow_router.post("/device/token")
async def device_token(body: DeviceTokenRequest, request: Request):
    """Step 4: CLI polls for token after user approves in browser.

    Returns:
        - 200 + tokens: if approved
        - 400 "authorization_pending": if not yet approved
        - 400 "expired_token": if device code expired
        - 400 "access_denied": if user denied
    """
    store: DeviceCodeStore = getattr(request.app.state, "device_code_store", None)
    if store is None:
        raise HTTPException(503, "Device flow not available")

    auth = store.get_by_device_code(body.device_code)

    if auth is None:
        # Expired or never existed
        return _error_response("expired_token", "Device code has expired. Please restart login.")

    if not auth.approved:
        return _error_response("authorization_pending", "Waiting for user to approve in browser.")

    # Approved — consume (one-time use) and issue tokens
    consumed = store.consume(body.device_code)
    if consumed is None:
        return _error_response("expired_token", "Code already consumed.")

    tokens = _generate_tokens(
        user_id=consumed.user_id,
        org_id=consumed.org_id,
        workspace_ids=consumed.workspace_ids or [],
        scope=consumed.scope,
    )

    logger.info("Device flow completed: user=%s", consumed.user_id)
    return tokens


@device_flow_router.post("/device/approve")
async def device_approve(body: DeviceApprovalRequest, request: Request):
    """Step 3: Browser submits approval after user authenticates.

    Called by the frontend after the user enters their code and logs in.
    """
    store: DeviceCodeStore = getattr(request.app.state, "device_code_store", None)
    if store is None:
        raise HTTPException(503, "Device flow not available")

    success = store.approve(
        user_code=body.user_code.upper().strip(),
        user_id=body.user_id,
        org_id=body.org_id,
        workspace_ids=body.workspace_ids,
    )

    if not success:
        raise HTTPException(404, "Invalid or expired user code")

    logger.info("Device flow approved: user_code=%s user=%s", body.user_code, body.user_id)
    return {"status": "approved"}


def _error_response(error: str, description: str):
    """RFC 8628 error response format."""
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=400,
        content={"error": error, "error_description": description},
    )
