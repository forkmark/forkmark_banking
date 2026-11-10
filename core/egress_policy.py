"""Data residency and network egress policy enforcement.

Enforces sovereign banking guardrails: external cloud LLM egress is blocked by
default unless FM_ALLOW_EXTERNAL_LLM_EGRESS is explicitly enabled, ensuring
bank outputs do not leave on-soil premises inadvertently.
"""
from __future__ import annotations

import ipaddress
import logging
import os
from urllib.parse import urlparse

logger = logging.getLogger("forkmark.egress")


class EgressBlocked(Exception):
    """Raised when an outbound model call to an external endpoint is blocked by egress policy."""
    def __init__(self, message: str = "External model egress is disabled (FM_ALLOW_EXTERNAL_LLM_EGRESS)"):
        super().__init__(message)


def _is_internal_host(hostname: str) -> bool:
    """True if hostname resolves to an on-premise, internal, or loopback network."""
    if not hostname:
        return False
    hostname = hostname.lower().strip()

    # Custom internal hostnames or domain suffixes configured by the bank
    extra_internal = os.getenv("FM_INTERNAL_LLM_HOSTS", "").lower().split(",")
    for host in extra_internal:
        h = host.strip()
        if h and (hostname == h or hostname.endswith(f".{h}")):
            return True

    # Standard loopback identifiers
    if hostname in ("localhost", "localhost.localdomain"):
        return True

    # Single-label names without dots (Docker / Kubernetes internal service names, e.g. 'ollama', 'vllm')
    if "." not in hostname:
        return True

    # Private internal domain suffixes
    internal_suffixes = (
        ".local",
        ".internal",
        ".cluster.local",
        ".corp",
        ".lan",
        ".home",
        ".arpa",
        ".svc",
        ".svc.cluster.local",
    )
    if any(hostname.endswith(suffix) for suffix in internal_suffixes):
        return True

    # IP address check: RFC 1918 private subnets, loopback, or link-local
    try:
        ip = ipaddress.ip_address(hostname)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        pass

    return False


def _is_external_endpoint(base_url: str) -> bool:
    """True if endpoint routes outside internal/private networks (i.e. public cloud).

    Uses a strict allowlist of internal/private hosts (localhost, single-label K8s services,
    RFC 1918 private IPs, .internal, .cluster.local, etc.). Any other public endpoint
    (e.g., api.openai.com, api.anthropic.com, *.openai.azure.com, openrouter.ai,
    generativelanguage.googleapis.com) is treated as external cloud egress and refused
    unless FM_ALLOW_EXTERNAL_LLM_EGRESS=true.
    """
    if not base_url:
        return True
    try:
        target = base_url if "://" in base_url else f"http://{base_url}"
        parsed = urlparse(target)
        hostname = parsed.hostname
        if not hostname:
            return True
        return not _is_internal_host(hostname)
    except Exception:
        return True


# Public aliases
is_internal_host = _is_internal_host
is_external_endpoint = _is_external_endpoint


def check_egress(base_url: str) -> None:
    """Check if outbound request to base_url is allowed.

    Raises EgressBlocked if the host is external and FM_ALLOW_EXTERNAL_LLM_EGRESS is not enabled.
    """
    allow_external = os.getenv("FM_ALLOW_EXTERNAL_LLM_EGRESS", "false").lower() in ("true", "1", "yes")
    if _is_external_endpoint(base_url) and not allow_external:
        logger.warning(
            "External LLM egress to '%s' blocked by sovereign data residency policy. "
            "Set FM_ALLOW_EXTERNAL_LLM_EGRESS=true to permit off-premise LLM scoring.",
            base_url,
        )
        raise EgressBlocked()

