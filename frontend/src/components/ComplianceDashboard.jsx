import { useState, useEffect } from 'react'
import { api, dispatchApiError } from '../api.js'
import { StatCard, PageHeader } from './ui'
import { pageStyle, panel, panelHeader, tableStyles as T } from './ui/styles.js'

// ── Coverage helpers ─────────────────────────────────────────────────────────

const COVERAGE = {
  COMPLETE: { label: 'COMPLETE', color: 'var(--green)',  bg: 'rgba(74,222,128,0.14)' },
  PARTIAL:  { label: 'PARTIAL',  color: 'var(--orange)', bg: 'rgba(251,191,36,0.14)' },
  MISSING:  { label: 'MISSING',  color: 'var(--red)',    bg: 'rgba(248,113,113,0.14)' },
}

const RISK_COLORS = {
  LOW: 'var(--muted)', MEDIUM: 'var(--orange)', HIGH: '#f97316', CRITICAL: 'var(--red)',
}

function cellStatus(model, framework) {
  const required = framework.required_artifacts || []
  if (required.length === 0) return null
  const present = new Set(model.present_artifacts || [])
  const have = required.filter(a => present.has(a)).length
  if (have === required.length) return 'COMPLETE'
  if (have === 0) return 'MISSING'
  return 'PARTIAL'
}

function fmtDate(iso) {
  if (!iso) return '—'
  try { return new Date(iso).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }) }
  catch { return iso }
}

function daysUntil(iso) {
  if (!iso) return null
  return Math.round((new Date(iso).getTime() - Date.now()) / 86400000)
}

const S = {
  cards: { display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 12, marginBottom: 24 },
  grid2: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginBottom: 20 },
  body: { padding: 16 },
  covRow: { display: 'flex', alignItems: 'center', gap: 12, marginBottom: 12 },
  covName: { width: 190, fontSize: 12, color: 'var(--text)', fontWeight: 600 },
  covBarWrap: { flex: 1, height: 8, background: 'var(--surface2)', borderRadius: 5, overflow: 'hidden' },
  covBar: (pct) => ({ width: `${pct}%`, height: '100%', background: pct >= 80 ? 'var(--green)' : pct >= 50 ? 'var(--orange)' : 'var(--red)' }),
  covPct: { width: 44, textAlign: 'right', fontSize: 12, fontWeight: 700, color: 'var(--text)' },
  cell: (status) => {
    const c = status ? COVERAGE[status] : { color: 'var(--muted)', bg: 'transparent' }
    return { color: c.color, background: c.bg, fontSize: 10, fontWeight: 700, padding: '3px 8px', borderRadius: 6, textAlign: 'center', letterSpacing: '0.03em', display: 'inline-block', minWidth: 68 }
  },
  timelineItem: { display: 'flex', alignItems: 'center', gap: 12, padding: '10px 0', borderBottom: '1px solid var(--border)' },
  dueDot: (overdue) => ({ width: 8, height: 8, borderRadius: '50%', background: overdue ? 'var(--red)' : 'var(--orange)', flexShrink: 0 }),
  emptyBody: { padding: '40px 24px', textAlign: 'center', color: 'var(--muted)', fontSize: 13, lineHeight: 1.6 },
  link: { color: 'var(--accent)', cursor: 'pointer', background: 'none', border: 'none', fontSize: 13, fontWeight: 600, textDecoration: 'underline', padding: 0, fontFamily: 'inherit' },
  riskDot: (tier) => ({ display: 'inline-block', width: 8, height: 8, borderRadius: 2, background: RISK_COLORS[tier] || 'var(--muted)', marginRight: 6 }),
}

export default function ComplianceDashboard({ nav }) {
  const [models, setModels] = useState([])
  const [frameworks, setFrameworks] = useState([])
  const [due, setDue] = useState([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let active = true
    async function load() {
      try {
        const [m, f, d] = await Promise.all([
          api.listModels(),
          api.listFrameworks(),
          api.modelsDueForRevalidation(30),
        ])
        if (!active) return
        setModels(m || [])
        setFrameworks(f || [])
        setDue(d || [])
      } catch (e) {
        dispatchApiError(e.message || 'Failed to load compliance dashboard')
      } finally {
        if (active) setLoading(false)
      }
    }
    load()
    return () => { active = false }
  }, [])

  if (loading) {
    return <div style={{ padding: 40, color: 'var(--muted)', fontSize: 13 }}>Loading…</div>
  }

  const activeModels = models.filter(m => m.status === 'ACTIVE')
  const fwById = Object.fromEntries(frameworks.map(f => [f.framework, f]))

  // Open findings = missing required artifacts across active models' applicable frameworks.
  let openFindings = 0
  for (const m of activeModels) {
    for (const fwId of m.regulatory_frameworks || []) {
      const fw = fwById[fwId]
      if (!fw) continue
      const present = new Set(m.present_artifacts || [])
      openFindings += (fw.required_artifacts || []).filter(a => !present.has(a)).length
    }
  }

  // Per-framework coverage %: share of applicable active models that are COMPLETE.
  const frameworkCoverage = frameworks.map(fw => {
    const applicable = activeModels.filter(m => (m.regulatory_frameworks || []).includes(fw.framework))
    const complete = applicable.filter(m => cellStatus(m, fw) === 'COMPLETE').length
    const pct = applicable.length ? Math.round((complete / applicable.length) * 100) : null
    return { fw, applicable: applicable.length, complete, pct }
  })
  const overallPcts = frameworkCoverage.filter(c => c.pct != null).map(c => c.pct)
  const avgCoverage = overallPcts.length
    ? Math.round(overallPcts.reduce((a, b) => a + b, 0) / overallPcts.length)
    : null

  // Only show framework columns that at least one active model is subject to.
  const usedFrameworks = frameworks.filter(fw =>
    activeModels.some(m => (m.regulatory_frameworks || []).includes(fw.framework)))

  const dueSorted = [...due].sort((a, b) => {
    const da = a.next_validation_due ? new Date(a.next_validation_due).getTime() : 0
    const db = b.next_validation_due ? new Date(b.next_validation_due).getTime() : 0
    return da - db
  })

  return (
    <div style={pageStyle(1150)}>
      <PageHeader
        title="Compliance Dashboard"
        subtitle="Model risk posture across your regulated model inventory"
      />

      {models.length === 0 ? (
        <div style={panel}>
          <div style={S.emptyBody}>
            <div style={{ fontSize: 15, fontWeight: 700, color: 'var(--text)', marginBottom: 8 }}>
              No models in the inventory yet
            </div>
            <div style={{ maxWidth: 460, margin: '0 auto 16px' }}>
              Register the models your institution uses to begin tracking their risk
              tier, applicable regulatory frameworks, and validation evidence.
            </div>
            <button style={S.link} onClick={() => nav('inventory')}>Go to Model Inventory →</button>
          </div>
        </div>
      ) : (
        <>
          <div style={S.cards}>
            <StatCard label="Models in Inventory" value={models.length} color="var(--accent)" />
            <StatCard label="Due for Revalidation (30d)" value={due.length}
                      color={due.length > 0 ? 'var(--orange)' : 'var(--green)'} />
            <StatCard label="Avg Framework Coverage" value={avgCoverage == null ? '—' : `${avgCoverage}%`}
                      color={avgCoverage != null && avgCoverage >= 80 ? 'var(--green)' : 'var(--orange)'} />
            <StatCard label="Open Findings" value={openFindings}
                      color={openFindings > 0 ? 'var(--red)' : 'var(--green)'} />
          </div>

          <div style={S.grid2}>
            {/* Framework coverage */}
            <div style={panel}>
              <div style={panelHeader}><span>Coverage by Framework</span></div>
              <div style={S.body}>
                {frameworkCoverage.map(({ fw, applicable, pct }) => (
                  <div key={fw.framework} style={S.covRow}>
                    <div style={S.covName} title={fw.name}>{fw.name.split('—')[0].trim()}</div>
                    <div style={S.covBarWrap}><div style={S.covBar(pct || 0)} /></div>
                    <div style={S.covPct}>{pct == null ? 'n/a' : `${pct}%`}</div>
                    <div style={{ fontSize: 11, color: 'var(--muted)', width: 70, textAlign: 'right' }}>
                      {applicable} model{applicable === 1 ? '' : 's'}
                    </div>
                  </div>
                ))}
              </div>
            </div>

            {/* Revalidation calendar */}
            <div style={panel}>
              <div style={panelHeader}><span>Revalidation Calendar</span></div>
              <div style={S.body}>
                {dueSorted.length === 0 ? (
                  <div style={{ color: 'var(--muted)', fontSize: 13, padding: '8px 0' }}>
                    No models are due for revalidation in the next 30 days.
                  </div>
                ) : dueSorted.map(m => {
                  const d = daysUntil(m.next_validation_due)
                  const overdue = d != null && d < 0
                  return (
                    <div key={m.model_id} style={S.timelineItem}>
                      <span style={S.dueDot(overdue)} />
                      <div style={{ flex: 1 }}>
                        <div style={{ fontSize: 13, fontWeight: 600 }}>{m.display_name}</div>
                        <div style={{ fontSize: 11, color: 'var(--muted)' }}>
                          {m.next_validation_due
                            ? `Due ${fmtDate(m.next_validation_due)}`
                            : 'Never validated'}
                        </div>
                      </div>
                      <div style={{ fontSize: 11, fontWeight: 700, color: overdue ? 'var(--red)' : 'var(--orange)' }}>
                        {m.next_validation_due == null ? 'UNVALIDATED'
                          : overdue ? `${Math.abs(d)}d overdue` : `in ${d}d`}
                      </div>
                    </div>
                  )
                })}
              </div>
            </div>
          </div>

          {/* Coverage matrix */}
          <div style={panel}>
            <div style={panelHeader}><span>Framework Coverage Matrix</span></div>
            <div style={{ overflowX: 'auto' }}>
              <table style={T.table}>
                <thead>
                  <tr>
                    <th style={T.th}>Model</th>
                    <th style={T.th}>Risk</th>
                    {usedFrameworks.map(fw => (
                      <th key={fw.framework} style={{ ...T.th, textAlign: 'center' }}
                          title={fw.name}>{fw.framework.toUpperCase().replace(/_/g, ' ')}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {activeModels.map(m => (
                    <tr key={m.model_id}>
                      <td style={T.td}>
                        <button style={S.link} onClick={() => nav('inventory')}>{m.display_name}</button>
                      </td>
                      <td style={T.td}>
                        <span style={S.riskDot(m.risk_tier)} />{m.risk_tier}
                      </td>
                      {usedFrameworks.map(fw => {
                        const subject = (m.regulatory_frameworks || []).includes(fw.framework)
                        const status = subject ? cellStatus(m, fw) : null
                        return (
                          <td key={fw.framework} style={{ ...T.td, textAlign: 'center' }}>
                            {subject
                              ? <span style={S.cell(status)}>{COVERAGE[status].label}</span>
                              : <span style={{ color: 'var(--muted)', fontSize: 12 }}>—</span>}
                          </td>
                        )
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
    </div>
  )
}
