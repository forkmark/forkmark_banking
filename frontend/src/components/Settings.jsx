import { useState, useEffect } from 'react'
import { api, dispatchApiError } from '../api.js'
import { PageHeader, pageStyle, SkeletonCard } from './ui'
import { S } from './settings/shared.jsx'
import ProfileSection from './settings/ProfileSection.jsx'
import LLMSection from './settings/LLMSection.jsx'
import ProviderSection from './settings/ProviderSection.jsx'
import InfraSection from './settings/InfraSection.jsx'

export default function Settings() {
  const [loading,  setLoading]  = useState(true)
  const [saving,   setSaving]   = useState(false)
  const [saved,    setSaved]    = useState(false)

  // current state from server
  const [keySet,   setKeySet]   = useState(false)
  const [keyMask,  setKeyMask]  = useState('')

  // form fields — LLM Provider
  const [apiKey,   setApiKey]   = useState('')
  const [baseUrl,  setBaseUrl]  = useState('')
  const [judgeModel, setJudgeModel] = useState('')
  const [customModel, setCustomModel] = useState('')

  // Divergence scorer
  const [scorer, setScorer]       = useState('auto')
  const [stModel, setStModel]     = useState('all-MiniLM-L6-v2')
  const [embedModel, setEmbedModel] = useState('text-embedding-3-small')

  // Appearance — institutional light theme is the default for regulated-finance use
  const [theme, setTheme] = useState('light')

  // Notifications
  const [notifToast, setNotifToast]       = useState(true)
  const [notifBrowser, setNotifBrowser]   = useState(false)
  const [notifDismiss, setNotifDismiss]   = useState(5)

  // User profile
  const [displayName, setDisplayName] = useState('')
  const [timezone, setTimezone]       = useState('')

  // system info
  const [storage,        setStorage]       = useState('sqlite')
  const [restartNeeded,  setRestartNeeded] = useState(false)

  // Background workers
  const [currentWorkers,  setCurrentWorkers]  = useState(4)
  const [selectedWorkers, setSelectedWorkers] = useState(4)

  // Enterprise features (read-only display)
  const [enterprise, setEnterprise] = useState({
    multi_tenant: false, scim_enabled: false,
    device_flow_enabled: false, otel_enabled: false,
    require_ui_auth: false,
  })

  useEffect(() => {
    Promise.all([
      api.getSettings(),
      api.getSystemInfo(),
    ])
      .then(([settings, sysInfo]) => {
        // LLM provider
        setKeySet(settings.openai_api_key_set || false)
        setKeyMask(settings.openai_api_key_masked || '')
        setBaseUrl(settings.openai_base_url || '')
        setJudgeModel(settings.judge_model || 'gpt-4o-mini')

        // Divergence scorer
        setScorer(settings.divergence_scorer || 'auto')
        setStModel(settings.st_model || 'all-MiniLM-L6-v2')
        setEmbedModel(settings.embed_model || 'text-embedding-3-small')

        // Appearance
        const savedTheme = settings.theme || 'light'
        setTheme(savedTheme)
        applyTheme(savedTheme)

        // Notifications
        setNotifToast(settings.notifications_toast !== 'false')
        setNotifBrowser(settings.notifications_browser === 'true')
        setNotifDismiss(parseInt(settings.notifications_auto_dismiss) || 5)

        // User profile
        setDisplayName(settings.display_name || '')
        setTimezone(settings.timezone || '')

        // System info
        setStorage(sysInfo.storage || (sysInfo.database_url_set ? 'postgresql' : 'sqlite'))

        const workers = sysInfo.background_workers || 4
        setCurrentWorkers(workers)
        setSelectedWorkers(workers)

        setEnterprise({
          multi_tenant: sysInfo.multi_tenant || false,
          scim_enabled: sysInfo.scim_enabled || false,
          device_flow_enabled: sysInfo.device_flow_enabled || false,
          otel_enabled: sysInfo.otel_enabled || false,
          require_ui_auth: sysInfo.require_ui_auth || false,
        })
      })
      .catch(() => dispatchApiError('Failed to load settings'))
      .finally(() => setLoading(false))
  }, [])

  function applyTheme(t) {
    const root = document.documentElement
    // Institutional light theme (default) and dark theme. Muted tones are tuned
    // to meet WCAG AA (≥4.5:1) for body text on their respective backgrounds.
    const palette = t === 'light' ? {
      '--bg':'#f7f8fa', '--surface':'#ffffff', '--surface2':'#eef1f6',
      '--border':'#d6dae3', '--accent':'#1f5fa8', '--green':'#157347',
      '--orange':'#b45309', '--red':'#b42318', '--purple':'#6d4aa3',
      '--text':'#16202e', '--muted':'#5a6473',
    } : {
      '--bg':'#0c0f1a', '--surface':'#161a2e', '--surface2':'#1c2138',
      '--border':'#2a3042', '--accent':'#7ba4f7', '--green':'#4ade80',
      '--orange':'#fbbf24', '--red':'#f87171', '--purple':'#c4a1f5',
      '--text':'#e6e9f2', '--muted':'#767f9c',
    }
    root.style.setProperty('color-scheme', t === 'light' ? 'light' : 'dark')
    Object.entries(palette).forEach(([k, v]) => root.style.setProperty(k, v))
  }

  function handleThemeChange(t) {
    setTheme(t)
    applyTheme(t)
  }

  async function save() {
    setSaving(true)
    setSaved(false)
    try {
      const body = {}
      if (apiKey.trim())       body.openai_api_key  = apiKey.trim()
      if (baseUrl.trim()) body.openai_base_url = baseUrl.trim()
      else if (baseUrl !== undefined) body.openai_base_url = null
      const jm = customModel.trim() || judgeModel
      if (jm) body.judge_model = jm

      // Divergence scorer
      body.divergence_scorer = scorer
      if (scorer === 'semantic') body.st_model = stModel
      if (scorer === 'openai')  body.embed_model = embedModel

      // Theme
      body.theme = theme

      // Notifications
      body.notifications_toast = notifToast ? 'true' : 'false'
      body.notifications_browser = notifBrowser ? 'true' : 'false'
      body.notifications_auto_dismiss = String(notifDismiss)

      // Profile
      body.display_name = displayName
      body.timezone = timezone

      await api.patchSettings(body)

      // Reload masked key from server
      const fresh = await api.getSettings()
      setKeySet(fresh.openai_api_key_set || false)
      setKeyMask(fresh.openai_api_key_masked || '')
      setApiKey('')  // clear input after save

      setSaved(true)
      window.dispatchEvent(new CustomEvent('fp:apisuccess', { detail: { message: 'Settings saved' } }))
      setTimeout(() => setSaved(false), 3000)
    } catch (e) {
      dispatchApiError(e.message || 'Failed to save settings')
    } finally {
      setSaving(false)
    }
  }

  async function saveSystemInfo(overrides = {}) {
    setSaving(true)
    try {
      const body = {}
      if (overrides.background_workers !== undefined) body.background_workers = overrides.background_workers
      if (overrides.require_ui_auth !== undefined) body.require_ui_auth = overrides.require_ui_auth

      await api.patchSystemInfo(body)
      setRestartNeeded(true)
      window.dispatchEvent(new CustomEvent('fp:apisuccess', {
        detail: { message: 'System setting saved — restart the server to apply.' }
      }))
    } catch (e) {
      dispatchApiError(e.message || 'Failed to save system setting')
    } finally {
      setSaving(false)
    }
  }

  if (loading) return (
    <div style={pageStyle(760)}>
      <SkeletonCard />
      <SkeletonCard />
    </div>
  )

  return (
    <div style={pageStyle(760)}>
      <PageHeader title="Settings" subtitle="Configure LLM providers, scoring, appearance, notifications, and platform infrastructure." />

      {/* Restart banner */}
      {restartNeeded && (
        <div style={S.banner} role="alert">
          <span style={S.bannerIcon}>⟳</span>
          <div>
            <strong>Restart required.</strong>{' '}
            One or more infrastructure settings have changed.
            Restart the ForkMark server for changes to take effect.
          </div>
        </div>
      )}

      <ProfileSection
        displayName={displayName} setDisplayName={setDisplayName}
        timezone={timezone} setTimezone={setTimezone}
        theme={theme} onThemeChange={handleThemeChange}
        notifToast={notifToast} setNotifToast={setNotifToast}
        notifBrowser={notifBrowser} setNotifBrowser={setNotifBrowser}
        notifDismiss={notifDismiss} setNotifDismiss={setNotifDismiss}
      />

      <ProviderSection />

      <LLMSection
        apiKey={apiKey} setApiKey={setApiKey} keySet={keySet} keyMask={keyMask}
        baseUrl={baseUrl} setBaseUrl={setBaseUrl}
        judgeModel={judgeModel} setJudgeModel={setJudgeModel}
        customModel={customModel} setCustomModel={setCustomModel}
        scorer={scorer} setScorer={setScorer}
        stModel={stModel} setStModel={setStModel}
        embedModel={embedModel} setEmbedModel={setEmbedModel}
      />

      <InfraSection
        saving={saving}
        storage={storage} restartNeeded={restartNeeded}
        currentWorkers={currentWorkers} selectedWorkers={selectedWorkers} setSelectedWorkers={setSelectedWorkers}
        enterprise={enterprise}
        onSaveSystemInfo={saveSystemInfo}
      />

      {/* Global save button */}
      <div style={S.footer}>
        <button
          style={S.btn(true, saving)}
          disabled={saving}
          onClick={save}
        >
          {saving ? 'Saving…' : 'Save Settings'}
        </button>
        {saved && <span style={{ fontSize:12, color:'var(--green)' }}>✓ Saved</span>}
      </div>
    </div>
  )
}
