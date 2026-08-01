"""Data residency controls — region-aware workspace routing.

Enterprise customers require data to stay in specific geographic regions
(GDPR, SOC2, HIPAA, etc.). This module enforces:

    1. Org-level region assignment (set during onboarding or SCIM provisioning)
    2. Workspace creation blocked if target region doesn't match org region
    3. Region-aware database routing (different PostgreSQL clusters per region)
    4. Cross-region access denied at middleware level

Supported regions:
    - us-east-1: US East (Virginia) — default
    - eu-west-1: EU West (Ireland) — GDPR
    - ap-southeast-1: Asia Pacific (Singapore)

Architecture:
    Each region has its own PostgreSQL cluster. The workspace router resolves
    which cluster to connect to based on org → region mapping.

Usage:
    residency = DataResidencyManager(config)

    # At workspace creation:
    residency.validate_region("org_acme", "eu-west-1")  # raises if mismatch

    # At connection time:
    db_url = residency.get_database_url("org_acme")  # returns region-specific URL
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Dict, List, Optional

logger = logging.getLogger("forkpoint.residency")


# ---------------------------------------------------------------------------
# Region definitions
# ---------------------------------------------------------------------------

@dataclass
class RegionConfig:
    """Configuration for a deployment region."""
    id: str
    name: str
    database_url_env: str    # env var holding the region's DB URL
    redis_url_env: str       # env var holding the region's Redis URL
    available: bool = True


REGIONS: Dict[str, RegionConfig] = {
    "us-east-1": RegionConfig(
        id="us-east-1",
        name="US East (Virginia)",
        database_url_env="FP_DATABASE_URL_US_EAST",
        redis_url_env="FP_REDIS_URL_US_EAST",
    ),
    "eu-west-1": RegionConfig(
        id="eu-west-1",
        name="EU West (Ireland)",
        database_url_env="FP_DATABASE_URL_EU_WEST",
        redis_url_env="FP_REDIS_URL_EU_WEST",
    ),
    "ap-southeast-1": RegionConfig(
        id="ap-southeast-1",
        name="Asia Pacific (Singapore)",
        database_url_env="FP_DATABASE_URL_AP_SOUTHEAST",
        redis_url_env="FP_REDIS_URL_AP_SOUTHEAST",
    ),
}

DEFAULT_REGION = "us-east-1"


# ---------------------------------------------------------------------------
# Data residency manager
# ---------------------------------------------------------------------------

class DataResidencyManager:
    """Manages region assignments and routes connections accordingly."""

    def __init__(self, default_database_url: str = "", default_redis_url: str = ""):
        self._default_db_url = default_database_url
        self._default_redis_url = default_redis_url
        self._org_regions: Dict[str, str] = {}  # org_id → region_id (cached)
        self._router = None  # set via set_router()

    def set_router(self, router):
        """Inject workspace router for DB lookups."""
        self._router = router

    def get_available_regions(self) -> List[Dict]:
        """List regions available in this deployment."""
        available = []
        for region_id, config in REGIONS.items():
            db_url = os.getenv(config.database_url_env, "")
            available.append({
                "id": region_id,
                "name": config.name,
                "available": bool(db_url) or region_id == DEFAULT_REGION,
            })
        return available

    def get_org_region(self, org_id: str) -> str:
        """Get the assigned region for an org."""
        # Check cache
        if org_id in self._org_regions:
            return self._org_regions[org_id]

        # Lookup from DB
        if self._router:
            try:
                with self._router.control_plane_connection() as conn:
                    conn.execute("SET search_path TO public")
                    row = conn.fetchone(
                        "SELECT region FROM organizations WHERE id = ?",
                        (org_id,),
                    )
                    if row:
                        region = dict(row).get("region", DEFAULT_REGION)
                        self._org_regions[org_id] = region
                        return region
            except Exception as e:
                logger.warning("Failed to lookup org region: %s", e)

        return DEFAULT_REGION

    def set_org_region(self, org_id: str, region_id: str):
        """Assign a region to an org. Only valid at org creation or by admin."""
        if region_id not in REGIONS:
            raise ValueError(f"Unknown region: {region_id}. Valid: {list(REGIONS.keys())}")

        # Verify region is available
        config = REGIONS[region_id]
        db_url = os.getenv(config.database_url_env, "")
        if not db_url and region_id != DEFAULT_REGION:
            raise ValueError(
                f"Region '{region_id}' not configured in this deployment. "
                f"Set {config.database_url_env} environment variable."
            )

        if self._router:
            try:
                with self._router.control_plane_connection() as conn:
                    conn.execute("SET search_path TO public")
                    conn.execute(
                        "UPDATE organizations SET region = ? WHERE id = ?",
                        (region_id, org_id),
                    )
            except Exception as e:
                logger.error("Failed to set org region: %s", e)
                raise

        self._org_regions[org_id] = region_id
        logger.info("Org %s assigned to region %s", org_id, region_id)

    def validate_region(self, org_id: str, requested_region: str):
        """Validate that a workspace creation request matches org's region.

        Raises PermissionError if regions don't match.
        """
        org_region = self.get_org_region(org_id)
        if requested_region and requested_region != org_region:
            raise PermissionError(
                f"Org '{org_id}' is bound to region '{org_region}'. "
                f"Cannot create workspace in '{requested_region}'. "
                f"Contact support to request a region migration."
            )

    def get_database_url(self, org_id: str) -> str:
        """Get the region-specific database URL for an org."""
        region_id = self.get_org_region(org_id)
        config = REGIONS.get(region_id)

        if config:
            url = os.getenv(config.database_url_env, "")
            if url:
                return url

        # Fallback to default
        return self._default_db_url or os.getenv("FP_DATABASE_URL", "")

    def get_redis_url(self, org_id: str) -> str:
        """Get the region-specific Redis URL for an org."""
        region_id = self.get_org_region(org_id)
        config = REGIONS.get(region_id)

        if config:
            url = os.getenv(config.redis_url_env, "")
            if url:
                return url

        return self._default_redis_url or os.getenv("FP_REDIS_URL", "")

    def check_cross_region_access(self, org_id: str, target_workspace_org_id: str) -> bool:
        """Check if cross-region access is attempted (should be denied).

        Returns True if access is same-region (allowed), False if cross-region.
        """
        source_region = self.get_org_region(org_id)
        target_region = self.get_org_region(target_workspace_org_id)
        return source_region == target_region


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_residency: Optional[DataResidencyManager] = None


def get_residency_manager(
    default_database_url: str = "",
    default_redis_url: str = "",
) -> DataResidencyManager:
    """Get or create the singleton data residency manager."""
    global _residency
    if _residency is None:
        _residency = DataResidencyManager(default_database_url, default_redis_url)
    return _residency
