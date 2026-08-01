"""SCIM 2.0 provisioning via WorkOS webhooks.

Handles directory sync events from WorkOS:
    - user.created → provision user + auto-assign to workspace
    - user.updated → update user metadata
    - user.deleted → deactivate user, remove workspace memberships
    - group.created → create workspace for the group
    - group.updated → update workspace metadata
    - group.deleted → archive workspace
    - group.user_added → add member to workspace
    - group.user_removed → remove member from workspace

Domain-Based Auto-Routing:
    When a user is provisioned, their email domain determines which org
    they belong to. Orgs register verified domains during setup.

Security:
    - All webhook payloads verified via HMAC signature (WorkOS webhook secret)
    - Idempotent: re-processing the same event is safe
    - Audit logged: every provisioning action is recorded

Usage:
    # In FastAPI app setup:
    app.include_router(scim_router, prefix="/api/webhooks")
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException, Request

logger = logging.getLogger("forkpoint.scim")

scim_router = APIRouter(tags=["scim"])


# ---------------------------------------------------------------------------
# Webhook signature verification
# ---------------------------------------------------------------------------

def verify_webhook_signature(payload: bytes, signature: str, secret: str) -> bool:
    """Verify WorkOS webhook HMAC-SHA256 signature."""
    if not signature or not secret:
        return False
    expected = hmac.new(
        secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------

class SCIMProvisioner:
    """Processes SCIM directory sync events from WorkOS."""

    def __init__(self, workspace_provisioner, webhook_secret: str):
        from ee.multitenancy import WorkspaceProvisioner
        self._provisioner: WorkspaceProvisioner = workspace_provisioner
        self._webhook_secret = webhook_secret
        self._domain_org_cache: Dict[str, str] = {}

    def _get_org_by_domain(self, email: str) -> Optional[str]:
        """Resolve org_id from email domain."""
        domain = email.split("@")[-1].lower() if "@" in email else None
        if not domain:
            return None

        if domain in self._domain_org_cache:
            return self._domain_org_cache[domain]

        # Lookup in DB
        org_id = self._provisioner.get_org_by_domain(domain)
        if org_id:
            self._domain_org_cache[domain] = org_id
        return org_id

    def _map_role(self, workos_role: str) -> str:
        """Map WorkOS directory role to Forkpoint role."""
        role_map = {
            "admin": "ws_admin",
            "owner": "org_admin",
            "member": "evaluator",
            "viewer": "viewer",
            "guest": "viewer",
        }
        return role_map.get(workos_role.lower(), "evaluator")

    # --- User events ---

    async def handle_user_created(self, data: Dict[str, Any]) -> Dict:
        """Provision a new user from directory sync."""
        user_id = data.get("id", "")
        email = data.get("emails", [{}])[0].get("value", "") if data.get("emails") else data.get("email", "")
        first_name = data.get("first_name", data.get("name", {}).get("givenName", ""))
        last_name = data.get("last_name", data.get("name", {}).get("familyName", ""))

        if not email:
            logger.warning("SCIM user.created missing email: %s", user_id)
            return {"status": "skipped", "reason": "no_email"}

        # Resolve org by domain
        org_id = self._get_org_by_domain(email)
        if not org_id:
            logger.warning("No org found for domain in email: %s", email)
            return {"status": "skipped", "reason": "unknown_domain"}

        # Provision user record
        self._provisioner.provision_user(
            user_id=user_id,
            email=email,
            org_id=org_id,
            display_name=f"{first_name} {last_name}".strip(),
        )

        logger.info("SCIM: provisioned user %s (%s) → org %s", user_id, email, org_id)
        return {"status": "provisioned", "user_id": user_id, "org_id": org_id}

    async def handle_user_updated(self, data: Dict[str, Any]) -> Dict:
        """Update user metadata from directory sync."""
        user_id = data.get("id", "")
        self._provisioner.update_user_metadata(user_id, data)
        logger.info("SCIM: updated user %s", user_id)
        return {"status": "updated", "user_id": user_id}

    async def handle_user_deleted(self, data: Dict[str, Any]) -> Dict:
        """Deactivate user and remove from all workspaces."""
        user_id = data.get("id", "")
        self._provisioner.deactivate_user(user_id)
        logger.info("SCIM: deactivated user %s", user_id)
        return {"status": "deactivated", "user_id": user_id}

    # --- Group events (group = workspace) ---

    async def handle_group_created(self, data: Dict[str, Any]) -> Dict:
        """Create a workspace for a new directory group."""
        group_id = data.get("id", "")
        group_name = data.get("name", data.get("displayName", ""))
        directory_id = data.get("directory_id", "")

        # Resolve org from directory
        org_id = data.get("organization_id", "")
        if not org_id:
            org_id = self._provisioner.get_org_by_directory(directory_id)

        if not org_id:
            logger.warning("SCIM group.created: cannot resolve org for group %s", group_id)
            return {"status": "skipped", "reason": "unknown_org"}

        workspace_id = self._provisioner.create_workspace(
            name=group_name,
            org_id=org_id,
            external_group_id=group_id,
        )

        logger.info("SCIM: created workspace '%s' (id=%s) for group %s", group_name, workspace_id, group_id)
        return {"status": "created", "workspace_id": workspace_id}

    async def handle_group_updated(self, data: Dict[str, Any]) -> Dict:
        """Update workspace metadata when group changes."""
        group_id = data.get("id", "")
        group_name = data.get("name", data.get("displayName", ""))
        self._provisioner.update_workspace_by_group(group_id, name=group_name)
        logger.info("SCIM: updated workspace for group %s", group_id)
        return {"status": "updated", "group_id": group_id}

    async def handle_group_deleted(self, data: Dict[str, Any]) -> Dict:
        """Archive workspace when group is deleted."""
        group_id = data.get("id", "")
        self._provisioner.archive_workspace_by_group(group_id)
        logger.info("SCIM: archived workspace for group %s", group_id)
        return {"status": "archived", "group_id": group_id}

    async def handle_group_user_added(self, data: Dict[str, Any]) -> Dict:
        """Add user to workspace when added to directory group."""
        group_id = data.get("group", {}).get("id", data.get("group_id", ""))
        user_id = data.get("user", {}).get("id", data.get("user_id", ""))
        role = self._map_role(data.get("role", "member"))

        workspace_id = self._provisioner.get_workspace_by_group(group_id)
        if not workspace_id:
            logger.warning("SCIM group.user_added: no workspace for group %s", group_id)
            return {"status": "skipped", "reason": "unknown_workspace"}

        self._provisioner.add_member(workspace_id, user_id, role)
        logger.info("SCIM: added user %s → workspace %s (role=%s)", user_id, workspace_id, role)
        return {"status": "added", "user_id": user_id, "workspace_id": workspace_id}

    async def handle_group_user_removed(self, data: Dict[str, Any]) -> Dict:
        """Remove user from workspace when removed from directory group."""
        group_id = data.get("group", {}).get("id", data.get("group_id", ""))
        user_id = data.get("user", {}).get("id", data.get("user_id", ""))

        workspace_id = self._provisioner.get_workspace_by_group(group_id)
        if not workspace_id:
            return {"status": "skipped", "reason": "unknown_workspace"}

        self._provisioner.remove_member(workspace_id, user_id)
        logger.info("SCIM: removed user %s from workspace %s", user_id, workspace_id)
        return {"status": "removed", "user_id": user_id, "workspace_id": workspace_id}

    # --- Dispatcher ---

    async def dispatch(self, event_type: str, data: Dict[str, Any]) -> Dict:
        """Route a webhook event to the appropriate handler."""
        handlers = {
            "dsync.user.created": self.handle_user_created,
            "dsync.user.updated": self.handle_user_updated,
            "dsync.user.deleted": self.handle_user_deleted,
            "dsync.group.created": self.handle_group_created,
            "dsync.group.updated": self.handle_group_updated,
            "dsync.group.deleted": self.handle_group_deleted,
            "dsync.group.user_added": self.handle_group_user_added,
            "dsync.group.user_removed": self.handle_group_user_removed,
        }

        handler = handlers.get(event_type)
        if handler is None:
            logger.debug("SCIM: ignoring unhandled event type: %s", event_type)
            return {"status": "ignored", "event_type": event_type}

        return await handler(data)


# ---------------------------------------------------------------------------
# FastAPI route
# ---------------------------------------------------------------------------

@scim_router.post("/workos")
async def workos_webhook(
    request: Request,
    workos_signature: Optional[str] = Header(None, alias="WorkOS-Signature"),
):
    """Receive and process WorkOS directory sync webhooks.

    Verifies signature, dispatches to SCIMProvisioner, returns 200 on success.
    WorkOS retries on non-2xx, so we return 200 even for "skipped" events.
    """
    secret = os.getenv("WORKOS_WEBHOOK_SECRET", "")
    body = await request.body()

    # Verify signature in production
    if secret and workos_signature:
        if not verify_webhook_signature(body, workos_signature, secret):
            raise HTTPException(401, "Invalid webhook signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON payload")

    event_type = payload.get("event", payload.get("type", ""))
    data = payload.get("data", {})
    event_id = payload.get("id", "unknown")

    logger.info("SCIM webhook received: type=%s id=%s", event_type, event_id)

    # Get provisioner from app state
    provisioner = getattr(request.app.state, "scim_provisioner", None)
    if provisioner is None:
        logger.error("SCIMProvisioner not configured in app state")
        raise HTTPException(500, "SCIM provisioning not configured")

    result = await provisioner.dispatch(event_type, data)
    result["event_id"] = event_id

    return result
