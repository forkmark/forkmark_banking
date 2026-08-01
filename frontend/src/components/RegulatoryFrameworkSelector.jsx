import { useState, useEffect } from 'react'
import { api } from '../api.js'
import { panel, panelHeader } from './ui/styles.js'

const S = {
  body: { padding: 14 },
  intro: { fontSize: 12, color: 'var(--muted)', marginBottom: 12, lineHeight: 1.5 },
  chips: { display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 12 },
  chip: (on) => ({
    fontSize: 12, padding: '6px 12px', borderRadius: 6, cursor: 'pointer',
    border: `1px solid ${on ? 'var(--accent)' : 'var(--border)'}`,
    background: on ? 'rgba(123,164,247,0.12)' : 'transparent',
    color: on ? 'var(--accent)' : 'var(--text)', fontWeight: 600,
  }),
  checklist: { marginTop: 8, borderTop: '1px solid var(--border)', paddingTop: 10 },
  fwName: { fontSize: 12, fontWeight: 700, color: 'var(--text)', margin: '8px 0 4px' },
  artifact: { display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: 'var(--muted)', padding: '2px 0' },
  tick: { color: 'var(--accent)', fontWeight: 700 },
}

export default function RegulatoryFrameworkSelector({ value = [], onChange }) {
  const [frameworks, setFrameworks] = useState([])

  useEffect(() => {
    let active = true
    api.listFrameworks().then(f => { if (active) setFrameworks(f || []) }).catch(() => {})
    return () => { active = false }
  }, [])

  function toggle(id) {
    const next = value.includes(id) ? value.filter(v => v !== id) : [...value, id]
    onChange && onChange(next)
  }

  const selected = frameworks.filter(f => value.includes(f.framework))

  return (
    <div style={panel}>
      <div style={panelHeader}><span>Regulatory Frameworks</span></div>
      <div style={S.body}>
        <div style={S.intro}>
          Select the framework(s) this comparison run is being conducted under. The
          required evidence artifacts for each are shown below.
        </div>
        <div style={S.chips}>
          {frameworks.map(f => (
            <button key={f.framework} type="button" style={S.chip(value.includes(f.framework))}
                    onClick={() => toggle(f.framework)} title={f.name}>
              {f.framework.toUpperCase().replace(/_/g, ' ')}
            </button>
          ))}
        </div>

        {selected.length > 0 && (
          <div style={S.checklist}>
            {selected.map(f => (
              <div key={f.framework}>
                <div style={S.fwName}>{f.name}</div>
                {(f.required_artifacts || []).map(a => (
                  <div key={a} style={S.artifact}>
                    <span style={S.tick}>▢</span>{a.replace(/_/g, ' ')}
                  </div>
                ))}
                {f.bias_test_required && (
                  <div style={{ ...S.artifact, color: 'var(--orange)' }}>
                    <span>⚠</span>Bias / fairness testing is mandatory under this framework.
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
