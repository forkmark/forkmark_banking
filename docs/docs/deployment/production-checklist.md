# Production Checklist

Use this checklist before deploying ForkMark to production.

## Security

- [ ] Set `FM_REQUIRE_UI_AUTH=true` to require API keys on all endpoints
- [ ] Generate and set `FM_SECRET_KEY` (Fernet key for encrypting sensitive settings)
- [ ] Generate and set `JWT_SIGNING_KEY` (for device flow authentication)
- [ ] Run behind HTTPS (TLS termination at reverse proxy)
- [ ] Restrict CORS origins via `FM_CORS_ORIGINS`
- [ ] Create workspace-scoped API keys (not org-wide) for SDK usage
- [ ] Review RBAC role assignments — use `sdk_only` for automated pipelines

## Database

- [ ] Use PostgreSQL 14+ with PgBouncer for multi-worker production. SQLite is strictly single-process and must NEVER be used across multiple worker processes (concurrent SQLite workers risk lock contention and audit rejection under supervisory load).
- [ ] Set `FM_DATABASE_URL` to your PostgreSQL connection string
- [ ] Apply sequence-partitioned tables for high-throughput supervisory audit logs: `psql -f scripts/postgres_partitioning.sql`
- [ ] Verify audit log tamper-evident hash chain integrity: `GET /api/audit/verify`
- [ ] Enable connection pooling via PgBouncer for >50 concurrent connections
- [ ] Configure `FM_PG_POOL_MIN` and `FM_PG_POOL_MAX` appropriately
- [ ] Set up automated backups
- [ ] Enable SSL for database connections

## Regional & Sovereign Data Residency

- [ ] Set `FM_DEFAULT_REGION=me-central-1` for UAE/GCC on-soil deployments
- [ ] Verify regional database isolation and ensure cross-region egress is blocked
- [ ] Verify local font assets / Cairo font rendering for air-gapped banking VPCs

## Caching and messaging

- [ ] (Optional) Set `FM_REDIS_URL` to enable Redis-backed caching
- [ ] Tune `FM_BACKGROUND_WORKERS` for in-process background scoring throughput

## Observability

- [ ] Set `FM_LOG_LEVEL=INFO` and `FM_LOG_FORMAT=json`
- [ ] Configure log aggregation (ELK, Datadog, etc.) — logs are structured JSON
- [ ] Set up OpenTelemetry if using distributed tracing: `FM_OTEL_ENABLED=true`
- [ ] Monitor `/metrics` endpoint for request latency and error rates
- [ ] Set up alerts on `/readyz` endpoint for Kubernetes readiness

## Multi-tenancy (enterprise edition — not in the OSS build)

> The items in this section apply to the planned enterprise edition only. They have no effect in the open-source build (v0.1.x).

- [ ] Set `FM_MULTI_TENANT=true`
- [ ] Configure SCIM provisioning via WorkOS
- [ ] Set up data residency regions if needed
- [ ] Mark non-production workspaces as sandbox

## Performance

- [ ] Stats endpoint is cached (15s TTL) — no action needed
- [ ] Rate limiting is enabled by default (1000 req/min per key)
- [ ] Configure divergence scorer based on your latency/cost/quality trade-off
- [ ] Use `lexical` or `semantic` scorer for high-throughput pipelines
- [ ] Use `llm_judge` scorer selectively for high-value comparisons

## Backup and recovery

- [ ] Automated PostgreSQL backups (pg_dump or WAL archiving)
- [ ] Redis persistence enabled (RDB or AOF)
- [ ] Test restore procedure
- [ ] Document and test disaster recovery plan
