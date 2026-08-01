import { useState, useEffect } from 'react'
import { api, dispatchApiError, postDownloadFile } from '../api.js'
import { PageHeader } from './ui'
import {
  pageStyle, panel, panelHeader, tableStyles as T,
  btnPrimary, btnSecondary, modalStyles as M, formStyles as F,
} from './ui/styles.js'

const RISK = {
  LOW:      { color: 'var(--muted)',  bg: 'rgba(107,115,148,0.14)' },
  MEDIUM:   { color: 'var(--orange)', bg: 'rgba(251,191,36,0.14)' },
  HIGH:     { color: '#f97316',       bg: 'rgba(249,115,22,0.14)' },
  CRITICAL: { color: 'var(--red)',    bg: 'rgba(248,113,113,0.14)' },
}
const STATUS = {
  ACTIVE:       { color: 'var(--green)',  bg: 'rgba(74,222,128,0.14)' },
  UNDER_REVIEW: { color: 'var(--orange)', bg: 'rgba(251,191,36,0.14)' },
  RETIRED:      { color: 'var(--muted)',  bg: 'rgba(107,115,148,0.14)' },
}
const RISK_TIERS = ['LOW', 'MEDIUM', 'HIGH', 'CRITICAL']
const STATUSES = ['ACTIVE', 'UNDER_REVIEW', 'RETIRED']

const badge = (map, key) => ({
  ...(map[key] || map[Object.keys(map)[0]]),
  fontSize: 10, fontWeight: 700, padding: '2px 8px', borderRadius: 10,
  letterSpacing: '0.03em', display: 'inline-block',
})

function toDateInput(iso) { return iso ? iso.slice(0, 10) : '' }
function fromDateInput(d) { return d ? new Date(d + 'T00:00:00Z').toISOString() : null }
function fmtDate(iso) {
  if (!iso) return '—'
  try { return new Date(iso).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }) }
  catch { return iso }
}

const emptyForm = () => ({
  model_id: '', display_name: '', provider: '', version: '', use_case: '',
  risk_tier: 'MEDIUM', regulatory_frameworks: [], deployed_at: toDateInput(new Date().toISOString()),
  owner_team: '', documentation_url: '', status: 'ACTIVE', last_validated_at: '',
  present_artifacts: [],
})

const S = {
  th: (active) => ({ ...T.th, cursor: 'pointer', userSelect: 'none', color: active ? 'var(--accent)' : 'var(--muted)' }),
  chkRow: { display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 10 },
  chk: (on) => ({
    fontSize: 11, padding: '4px 10px', borderRadius: 5, cursor: 'pointer',
    border: `1px solid ${on ? 'var(--accent)' : 'var(--border)'}`,
    background: on ? 'rgba(123,164,247,0.12)' : 'transparent',
    color: on ? 'var(--accent)' : 'var(--muted)', fontWeight: 600,
  }),
  reportBtn: { fontSize: 11, padding: '4px 10px', background: 'transparent', color: 'var(--accent)', border: '1px solid var(--border)', borderRadius: 5, cursor: 'pointer', fontWeight: 600 },
  actionCell: { display: 'flex', gap: 6, justifyContent: 'flex-end' },
  summaryBox: { background: 'var(--surface2)', border: '1px solid var(--border)', borderRadius: 6, padding: 12, fontSize: 12, lineHeight: 1.6, marginTop: 10 },
}

export default function ModelInventoryPage() {
  const [models, setModels] = useState([])
  const [frameworks, setFrameworks] = useState([])
  const [loading, setLoading] = useState(true)
  const [sortKey, setSortKey] = useState('display_name')
  const [sortDir, setSortDir] = useState(1)
  const [editing, setEditing] = useState(null)   // form object or null
  const [reportFor, setReportFor] = useState(null)

  async function load() {
    try {
      const [m, f] = await Promise.all([api.listModels(), api.listFrameworks()])
      setModels(m || [])
      setFrameworks(f || [])
    } catch (e) {
      dispatchApiError(e.message || 'Failed to load model inventory')
    } finally {
      setLoading(false)
    }
  }
  useEffect(() => { load() }, [])

  const artifactOptions = Array.from(
    new Set(frameworks.flatMap(f => f.required_artifacts || []))
  ).sort()

  function sortBy(key) {
    if (key === sortKey) setSortDir(d => -d)
    else { setSortKey(key); setSortDir(1) }
  }

  const sorted = [...models].sort((a, b) => {
    const va = (a[sortKey] ?? '').toString().toLowerCase()
    const vb = (b[sortKey] ?? '').toString().toLowerCase()
    return va < vb ? -sortDir : va > vb ? sortDir : 0
  })

  async function save() {
    const f = editing
    if (!f.model_id.trim() || !f.display_name.trim()) {
      dispatchApiError('Model ID and display name are required')
      return
    }
    const payload = {
      display_name: f.display_name, provider: f.provider, version: f.version,
      use_case: f.use_case, risk_tier: f.risk_tier,
      regulatory_frameworks: f.regulatory_frameworks,
      deployed_at: fromDateInput(f.deployed_at),
      owner_team: f.owner_team, documentation_url: f.documentation_url,
      status: f.status, last_validated_at: fromDateInput(f.last_validated_at),
      present_artifacts: f.present_artifacts,
    }
    try {
      if (f._isNew) await api.createModel({ model_id: f.model_id.trim(), ...payload })
      else await api.updateModel(f.model_id, payload)
      setEditing(null)
      setLoading(true)
      await load()
    } catch (e) {
      dispatchApiError(e.message || 'Failed to save model')
    }
  }

  async function remove(model_id) {
    try {
      await api.deleteModel(model_id)
      setModels(ms => ms.filter(m => m.model_id !== model_id))
    } catch (e) {
      dispatchApiError(e.message || 'Failed to delete model')
    }
  }

  const cols = [
    ['display_name', 'Model'], ['provider', 'Provider'], ['version', 'Version'],
    ['risk_tier', 'Risk'], ['status', 'Status'], ['next_validation_due', 'Next Validation'],
  ]

  if (loading) return <div style={{ padding: 40, color: 'var(--muted)', fontSize: 13 }}>Loading…</div>

  return (
    <div style={pageStyle(1150)}>
      <PageHeader title="Model Inventory" subtitle="System of record for models under governance" />

      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 14 }}>
        <button style={btnPrimary} onClick={() => setEditing({ ...emptyForm(), _isNew: true })}>
          + Add Model
        </button>
      </div>

      <div style={panel}>
        {sorted.length === 0 ? (
          <div style={{ padding: '40px 24px', textAlign: 'center', color: 'var(--muted)', fontSize: 13, lineHeight: 1.6 }}>
            <div style={{ fontSize: 15, fontWeight: 700, color: 'var(--text)', marginBottom: 8 }}>
              No models registered yet
            </div>
            Add each model your institution uses, tag its risk tier and applicable
            regulatory frameworks, and track its validation evidence here.
          </div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table style={T.table}>
              <thead>
                <tr>
                  {cols.map(([key, label]) => (
                    <th key={key} style={S.th(sortKey === key)} onClick={() => sortBy(key)}>
                      {label}{sortKey === key ? (sortDir === 1 ? ' ▲' : ' ▼') : ''}
                    </th>
                  ))}
                  <th style={{ ...T.th, textAlign: 'right' }}>Actions</th>
                </tr>
              </thead>
              <tbody>
                {sorted.map(m => (
                  <tr key={m.model_id}>
                    <td style={T.td}>
                      <div style={{ fontWeight: 600 }}>{m.display_name}</div>
                      <div style={{ fontSize: 11, color: 'var(--muted)' }}>{m.use_case || m.model_id}</div>
                    </td>
                    <td style={T.td}>{m.provider || '—'}</td>
                    <td style={T.td}>{m.version || '—'}</td>
                    <td style={T.td}><span style={badge(RISK, m.risk_tier)}>{m.risk_tier}</span></td>
                    <td style={T.td}><span style={badge(STATUS, m.status)}>{m.status.replace('_', ' ')}</span></td>
                    <td style={T.td}>{fmtDate(m.next_validation_due)}</td>
                    <td style={T.td}>
                      <div style={S.actionCell}>
                        <button style={S.reportBtn} onClick={() => setReportFor(m)}>Generate Report</button>
                        <button style={btnSecondary} onClick={() => setEditing({ ...toForm(m), _isNew: false })}>Edit</button>
                        <button style={{ ...btnSecondary, color: 'var(--red)', borderColor: 'var(--red)' }}
                                onClick={() => remove(m.model_id)}>Delete</button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {editing && (
        <ModelForm
          form={editing}
          setForm={setEditing}
          frameworks={frameworks}
          artifactOptions={artifactOptions}
          onCancel={() => setEditing(null)}
          onSave={save}
        />
      )}

      {reportFor && (
        <ReportModal model={reportFor} frameworks={frameworks} onClose={() => setReportFor(null)} />
      )}
    </div>
  )
}

function toForm(m) {
  return {
    model_id: m.model_id, display_name: m.display_name, provider: m.provider || '',
    version: m.version || '', use_case: m.use_case || '', risk_tier: m.risk_tier,
    regulatory_frameworks: m.regulatory_frameworks || [],
    deployed_at: toDateInput(m.deployed_at), owner_team: m.owner_team || '',
    documentation_url: m.documentation_url || '', status: m.status,
    last_validated_at: toDateInput(m.last_validated_at), present_artifacts: m.present_artifacts || [],
  }
}

function toggle(list, value) {
  return list.includes(value) ? list.filter(v => v !== value) : [...list, value]
}

function ModelForm({ form, setForm, frameworks, artifactOptions, onCancel, onSave }) {
  const set = (k, v) => setForm(f => ({ ...f, [k]: v }))
  return (
    <div style={M.overlay} onClick={onCancel}>
      <div style={M.box(560)} onClick={e => e.stopPropagation()}>
        <div style={M.title}>{form._isNew ? 'Add Model' : 'Edit Model'}</div>
        <div style={M.subtitle}>Register the model's risk tier, applicable frameworks, and validation status.</div>

        <div style={F.row2}>
          <div>
            <label style={F.label}>Model ID</label>
            <input style={F.input} value={form.model_id} disabled={!form._isNew}
                   onChange={e => set('model_id', e.target.value)} placeholder="credit-llm-v2" />
          </div>
          <div>
            <label style={F.label}>Display Name</label>
            <input style={F.input} value={form.display_name}
                   onChange={e => set('display_name', e.target.value)} placeholder="Credit Decision Assistant" />
          </div>
        </div>

        <div style={F.row2}>
          <div><label style={F.label}>Provider</label>
            <input style={F.input} value={form.provider} onChange={e => set('provider', e.target.value)} /></div>
          <div><label style={F.label}>Version</label>
            <input style={F.input} value={form.version} onChange={e => set('version', e.target.value)} /></div>
        </div>

        <label style={F.label}>Use Case</label>
        <input style={F.input} value={form.use_case} onChange={e => set('use_case', e.target.value)}
               placeholder="consumer credit adjudication" />

        <div style={F.row2}>
          <div><label style={F.label}>Risk Tier</label>
            <select style={F.select} value={form.risk_tier} onChange={e => set('risk_tier', e.target.value)}>
              {RISK_TIERS.map(t => <option key={t} value={t}>{t}</option>)}
            </select></div>
          <div><label style={F.label}>Status</label>
            <select style={F.select} value={form.status} onChange={e => set('status', e.target.value)}>
              {STATUSES.map(s => <option key={s} value={s}>{s.replace('_', ' ')}</option>)}
            </select></div>
        </div>

        <label style={{ ...F.label, marginTop: 10 }}>Regulatory Frameworks</label>
        <div style={S.chkRow}>
          {frameworks.map(f => (
            <button key={f.framework} type="button"
                    style={S.chk(form.regulatory_frameworks.includes(f.framework))}
                    onClick={() => set('regulatory_frameworks', toggle(form.regulatory_frameworks, f.framework))}>
              {f.framework.toUpperCase().replace(/_/g, ' ')}
            </button>
          ))}
        </div>

        <div style={F.row2}>
          <div><label style={F.label}>Deployed At</label>
            <input type="date" style={F.input} value={form.deployed_at} onChange={e => set('deployed_at', e.target.value)} /></div>
          <div><label style={F.label}>Last Validated At</label>
            <input type="date" style={F.input} value={form.last_validated_at} onChange={e => set('last_validated_at', e.target.value)} /></div>
        </div>

        <div style={F.row2}>
          <div><label style={F.label}>Owner Team</label>
            <input style={F.input} value={form.owner_team} onChange={e => set('owner_team', e.target.value)} /></div>
          <div><label style={F.label}>Documentation URL</label>
            <input style={F.input} value={form.documentation_url} onChange={e => set('documentation_url', e.target.value)} /></div>
        </div>

        {artifactOptions.length > 0 && (
          <>
            <label style={{ ...F.label, marginTop: 6 }}>Evidence Artifacts on File</label>
            <div style={S.chkRow}>
              {artifactOptions.map(a => (
                <button key={a} type="button" style={S.chk(form.present_artifacts.includes(a))}
                        onClick={() => set('present_artifacts', toggle(form.present_artifacts, a))}>
                  {a.replace(/_/g, ' ')}
                </button>
              ))}
            </div>
          </>
        )}

        <div style={M.footer}>
          <button style={btnSecondary} onClick={onCancel}>Cancel</button>
          <button style={btnPrimary} onClick={onSave}>{form._isNew ? 'Add Model' : 'Save Changes'}</button>
        </div>
      </div>
    </div>
  )
}

function ReportModal({ model, frameworks, onClose }) {
  const applicable = (model.regulatory_frameworks && model.regulatory_frameworks.length)
    ? model.regulatory_frameworks
    : frameworks.map(f => f.framework)
  const [framework, setFramework] = useState(applicable[0] || 'cbuae_mms')
  const [memo, setMemo] = useState(null)
  const [busy, setBusy] = useState(false)

  async function generate() {
    setBusy(true)
    try {
      const result = await api.generateComplianceReport(model.model_id, { framework })
      setMemo(result)
    } catch (e) {
      dispatchApiError(e.message || 'Failed to generate report')
    } finally {
      setBusy(false)
    }
  }

  async function downloadDocx() {
    setBusy(true)
    try {
      await postDownloadFile(
        `/api/compliance/reports/${model.model_id}/docx`,
        { framework },
        `validation_memo_${model.model_id}_${framework}.docx`,
      )
    } catch (e) {
      dispatchApiError(e.message || 'Failed to download report')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div style={M.overlay} onClick={onClose}>
      <div style={M.box(500)} onClick={e => e.stopPropagation()}>
        <div style={M.title}>Generate Validation Report</div>
        <div style={M.subtitle}>{model.display_name}</div>

        <label style={F.label}>Framework</label>
        <select style={F.select} value={framework} onChange={e => { setFramework(e.target.value); setMemo(null) }}>
          {applicable.map(fw => <option key={fw} value={fw}>{fw.toUpperCase().replace(/_/g, ' ')}</option>)}
        </select>

        {memo && (
          <div style={S.summaryBox}>
            <div><strong>Coverage:</strong> {memo.regulatory_mapping.present_count} present,{' '}
              {memo.regulatory_mapping.missing_count} missing{' '}
              ({memo.regulatory_mapping.coverage_complete ? 'complete' : 'incomplete'})</div>
            <div><strong>Findings:</strong> {memo.findings_and_recommendations.length}</div>
            <div style={{ marginTop: 6, color: 'var(--muted)' }}>
              {memo.findings_and_recommendations.slice(0, 3).map((f, i) => (
                <div key={i}>• [{f.severity}] {f.description}</div>
              ))}
            </div>
          </div>
        )}

        <div style={M.footer}>
          <button style={btnSecondary} onClick={onClose}>Close</button>
          <button style={btnSecondary} disabled={busy} onClick={generate}>
            {busy ? '…' : memo ? 'Regenerate' : 'Preview'}
          </button>
          <button style={btnPrimary} disabled={busy} onClick={downloadDocx}>Download .docx</button>
        </div>
      </div>
    </div>
  )
}
