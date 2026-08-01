import { useState, useEffect } from 'react'
import { api } from '../api.js'
import { panel, panelHeader } from './ui/styles.js'

// Cohen's (1988) magnitude thresholds.
function magnitude(d) {
  const a = Math.abs(d)
  if (a < 0.2) return 'negligible'
  if (a < 0.5) return 'small'
  if (a < 0.8) return 'medium'
  return 'large'
}

const S = {
  body: { padding: 16 },
  line: { fontSize: 13, color: 'var(--text)', marginBottom: 8, lineHeight: 1.5 },
  strong: { fontWeight: 700 },
  yes: { color: 'var(--green)', fontWeight: 700 },
  no: { color: 'var(--orange)', fontWeight: 700 },
  barWrap: { position: 'relative', height: 34, margin: '14px 0 6px', background: 'var(--surface2)', borderRadius: 6, border: '1px solid var(--border)' },
  band: (lo, hi) => ({ position: 'absolute', top: 0, bottom: 0, left: `${lo * 100}%`, width: `${(hi - lo) * 100}%`, background: 'rgba(123,164,247,0.22)', borderLeft: '1px solid var(--accent)', borderRight: '1px solid var(--accent)' }),
  point: (p) => ({ position: 'absolute', top: -3, bottom: -3, left: `calc(${p * 100}% - 1px)`, width: 2, background: 'var(--accent)' }),
  mid: { position: 'absolute', top: 0, bottom: 0, left: '50%', width: 1, background: 'var(--border)' },
  scale: { display: 'flex', justifyContent: 'space-between', fontSize: 10, color: 'var(--muted)' },
  warn: { marginTop: 12, padding: '10px 12px', background: 'rgba(251,191,36,0.10)', border: '1px solid rgba(251,191,36,0.3)', borderRadius: 6, fontSize: 12, color: 'var(--text)', lineHeight: 1.5 },
  empty: { padding: 16, fontSize: 13, color: 'var(--muted)', lineHeight: 1.6 },
}

export default function StatisticsPanel({ scoresA, scoresB, title = 'Statistical Analysis' }) {
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)

  const a = Array.isArray(scoresA) ? scoresA : []
  const b = Array.isArray(scoresB) ? scoresB : []
  const enough = a.length >= 2 && b.length >= 2 && a.length === b.length

  useEffect(() => {
    if (!enough) { setResult(null); return }
    let active = true
    setLoading(true)
    api.analyzeStatistics({ scores_a: a, scores_b: b })
      .then(r => { if (active) { setResult(r.results[0]); setError(null) } })
      .catch(e => { if (active) setError(e.message || 'Analysis failed') })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(a), JSON.stringify(b)])

  return (
    <div style={panel}>
      <div style={panelHeader}><span>{title}</span></div>
      {!enough ? (
        <div style={S.empty}>
          A statistical summary appears once at least two paired per-sample scores are
          available for each branch. Run this comparison across an evaluation set to
          populate win rate, significance, and effect size.
        </div>
      ) : loading ? (
        <div style={S.empty}>Computing…</div>
      ) : error ? (
        <div style={S.empty}>{error}</div>
      ) : result ? (
        <div style={S.body}>
          <div style={S.line}>
            <span style={S.strong}>Win rate:</span>{' '}
            {(result.win_rate * 100).toFixed(1)}%{' '}
            <span style={{ color: 'var(--muted)' }}>
              (95% CI: {(result.ci_lower * 100).toFixed(1)}%–{(result.ci_upper * 100).toFixed(1)}%)
            </span>
          </div>
          <div style={S.line}>
            <span style={S.strong}>Statistically significant:</span>{' '}
            {result.is_significant
              ? <span style={S.yes}>Yes</span>
              : <span style={S.no}>No</span>}{' '}
            <span style={{ color: 'var(--muted)' }}>
              (p = {result.adjusted_p_value.toFixed(3)}, Cohen&rsquo;s d = {result.effect_size.toFixed(2)} — {magnitude(result.effect_size)} effect)
            </span>
          </div>
          <div style={S.line}>
            <span style={S.strong}>Sample size:</span>{' '}
            {result.sample_size} comparison{result.sample_size === 1 ? '' : 's'}{' '}
            <span style={{ color: 'var(--muted)' }}>
              (min. detectable effect d = {result.minimum_detectable_effect.toFixed(2)} at power 0.80)
            </span>
          </div>

          {/* Confidence-interval bar with point estimate */}
          <div style={S.barWrap} title="95% Wilson confidence interval for the win rate">
            <div style={S.mid} />
            <div style={S.band(result.ci_lower, result.ci_upper)} />
            <div style={S.point(result.win_rate)} />
          </div>
          <div style={S.scale}><span>0%</span><span>50%</span><span>100%</span></div>

          {Math.abs(result.effect_size) < result.minimum_detectable_effect && (
            <div style={S.warn}>
              <strong>Underpowered:</strong> the observed effect (|d| = {Math.abs(result.effect_size).toFixed(2)})
              is below the minimum detectable effect ({result.minimum_detectable_effect.toFixed(2)}) at this
              sample size. Expand the evaluation set before drawing a firm conclusion.
            </div>
          )}
        </div>
      ) : null}
    </div>
  )
}
