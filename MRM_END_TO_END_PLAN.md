# ForkMark — Product Plan: AI/LLM Model Validation for Regulated Financial Institutions

**Positioning (the one line):** *Self-hosted AI and LLM model validation for regulated
financial institutions — the AI-specific validation and evidence layer for the model risks
your existing MRM framework doesn't cover, mapped first to the UAE regimes that bind you
(CBUAE), and to EU and US regimes as options.*

**Hero market: the UAE.** EU and US are secondary. We are not building a general-purpose MRM
platform — banks already have one. We plug into it and cover the AI/LLM gap it can't.

**Status:** proposal for review. No code changes until scope and sequencing are agreed.

---

## 1. What we are and are not

**We are:** a focused, self-hosted platform that validates AI/LLM models against regulatory
controls and produces the validation evidence a bank's MRM function and regulators require.

**We are not:** the bank's model inventory system of record, its MRM workflow engine, or a
generic "compare two prompts" tool. Those either already exist at the bank or are undifferentiated.

**The market is an intersection, not a category:**
`AI/LLM models` × `binding regulation (CBUAE first)` × `regulated financial institutions`.
Everything outside that intersection is scope we deliberately refuse.

---

## 2. Why the UAE is the hero (and why now)

The UAE is not just a friendly first market — its regulatory and technology reality is almost
purpose-built for a self-hosted, Arabic-capable, AI-specific validation tool. Four tailwinds:

1. **A unified, multi-regulator AI framework already exists.** CBUAE, SCA, DFSA (DIFC) and
   FSRA (ADGM) jointly issued the *Guidelines for Financial Institutions Adopting Enabling
   Technologies*, explicitly requiring governance, **model validation**, registries of material
   applications, version documentation, and vendor due diligence for AI. One country, one
   cross-regulator expectation we can map to end to end.
2. **A binding lifecycle standard + fresh AI guidance.** The binding CBUAE **Model Management
   Standards (2022)** governs the model lifecycle, and CBUAE's **Responsible AI guidance
   (Feb 2026)** adds AI-specific expectations: transparency, explainability, accountability that
   stays with the institution even when the model is a third-party/cloud vendor, and — critically —
   **consumer disclosures and explanations in both Arabic and English.**
3. **Banking data must stay onshore.** Under the UAE PDPL, banking data is subject to mandatory
   localization; transfer abroad needs Central Bank approval and customer consent, and CBUAE
   cloud-outsourcing guidance requires the institution to retain access and audit rights. **This
   makes the US-SaaS incumbents (ValidMind, Credo AI) a regulatory liability here and our
   self-hosted model a compliance feature, not a preference.**
4. **A sovereign Arabic-LLM boom the incumbents ignore.** Banks in the region increasingly deploy
   locally-built Arabic models — **Jais** (G42/Core42) and **Falcon** (TII, Abu Dhabi) — driven by
   data-sovereignty mandates. No global validation vendor validates these models or the Arabic /
   Gulf-dialect behavior they exhibit.

**The US "why now" (secondary but powerful):** as of April 2026, **SR 11-7 was replaced by
SR 26-2** — and generative/agentic AI is *explicitly excluded* from it. The primary US model-risk
standard now leaves a GenAI gap the regulator itself acknowledges. (Action item: our product still
frames SR 11-7 as foundational — update to SR 26-2 and lead with the GenAI-exclusion gap.)

---

## 3. Competitive reality and our moat (be honest about this)

The "AI-validation layer that integrates with your MRM/GRC" category is **already contested** by
better-funded players: **ValidMind** (200+ tests, SR 11-7/SS1-23/EU AI Act templates, Experian
partnership) and **Credo AI** (Gartner-recognized AI governance, GRC/model-registry integrations).
Positioning alone will not differentiate us. Our defensibility must rest on things they
structurally do not do:

- **Self-hosted / in-country by design** — answers UAE banking-data localization and CBUAE
  cloud-access/audit requirements that SaaS cannot cleanly meet.
- **UAE-native regulatory depth** — CBUAE MMS + CBUAE AI guidance + the joint Enabling-Tech
  Guidelines + DFSA + FSRA, in one evidence pack. Incumbents map to US/EU.
- **Arabic and sovereign-model validation** — Gulf-dialect LLM behavior, Jais/Falcon presets, and
  bilingual (Arabic/English) evidence that CBUAE now mandates.
- **Founder credibility** — Wells Fargo MRM quant; validation science is the product, not a wrapper.

If we are not visibly deeper than the incumbents on these four, we are a thinner clone. The rest of
this plan is about building that depth and cutting everything that isn't it.

---

## 4. The product spine (what we build)

Reusable foundations already in the codebase: a real evidence-assembling memo generator
(`core/compliance_reporter.py`), working evaluators (`core/finance_evaluators.py`), paired
statistics (`core/statistical_analyzer.py`), RBAC + audit log, and a denormalized `eval_run_id`
across comparisons/decisions/runs. The keystone gap: evaluations aren't linked to a governed model.

The spine, in dependency order — **the AI-validation test library (Phase 2) is the core of the
product, not the A/B comparison.**

### Phase 0 — Keystone: link evaluations to a governed AI model · Effort **M**
- **Data:** migration **v13** — nullable `governed_model_id` on `eval_runs`
  (FK → `model_inventory.model_id`), indexed; backward compatible.
- **Backend:** thread `governed_model_id` through eval-run creation and the SDK; add rollups
  (`list_eval_runs_for_model`, `_decisions_for_model`, evidence-bundle-for-model).
- **Outcome:** all validation activity attaches to the AI model under governance. Enabler for
  everything below.

### Phase 1 — Evidence-backed, bilingual validation memo · Effort **M** · depends on 0
- **Backend:** `_build_evidence_from_model(model_id)` auto-pulls the model's linked statistics,
  evaluator results, and human-review decisions into `ComplianceReporter` (no more hand-fed evidence).
- **Bilingual:** generate the memo and consumer-explainability artifacts in **Arabic and English**
  (CBUAE mandate) — a template + translation pass on the memo sections.
- **Outcome:** "Generate Validation Report" produces a real, regulator-ready, bilingual memo. This
  alone removes most of the "it's just a comparison tool" perception.

### Phase 2 — AI/LLM validation test library mapped to regulation · Effort **L** · **the core**
This is the differentiator and the moat. A structured, extensible library of **LLM-specific
validation tests**, each tied to a regulatory control, run against a governed model:
- **Factuality / hallucination / RAG grounding** (extends the existing numerical-fidelity evaluator).
- **Prompt robustness & output consistency** under paraphrase/perturbation (non-determinism — the
  exact SR 26-2 GenAI gap).
- **Bias & fairness in generative text** across protected attributes (extends bias evaluator).
- **Safety / jailbreak / prompt-injection red-teaming.**
- **Drift for third-party-updated models** (models that change after deployment).
- **Arabic / Gulf-dialect suite** — dialect handling (MSA vs Gulf/Levantine/Egyptian),
  Arabic-English code-switching, RTL, cultural/religious sensitivity, Arabic explainability quality.
- **Sovereign-model presets** — ready-made validation profiles for **Jais** and **Falcon** so a
  bank can validate the models it actually runs, out of the box.
- Each test result flows into the Phase 1 evidence bundle and maps to a CBUAE/EU/US requirement.
- **Outcome:** ForkMark validates AI models the way the incumbents claim to but in Arabic, for
  sovereign models, self-hosted — depth a technical evaluator can see in minutes.

### Phase 3 — UAE regulatory pack + evidence-export / integration surface · Effort **M** · depends 0–2
- **UAE regulatory pack:** coverage mapping + memo templates for CBUAE MMS, CBUAE AI guidance, the
  joint Enabling-Tech Guidelines, DFSA and FSRA — sold as the "UAE pack"; EU AI Act and SR 26-2 as
  additional packs.
- **Integration = evidence out, not bespoke connectors:** a clean export layer (validation-evidence
  pack: bilingual memo `.docx` + structured JSON + coverage + test results), a documented API, and
  standard artifact formats the bank's existing MRM/GRC ingests. "Integrates with your MRM" means
  *feeds it evidence*, and stands alone where no platform exists.
- **Outcome:** the "plugs into your MRM" promise is real and demonstrable without touching a bank's
  proprietary system.

### Phase 4 — Focus cuts, reframed surface, and spine-complete UAE demos · Effort **M**
- Reframe nav/vocabulary around AI-model validation (see §5 cuts).
- Rebuild demos so one click seeds the whole story for a **UAE** scenario: a governed AI model
  (e.g., an Arabic retail-banking assistant on Jais) → validation test suite run → decisions →
  bilingual evidence-backed memo → CBUAE coverage. Demos must land with a governance audience.

---

## 5. Focus cuts — what we remove to show we're razor-focused

Being focused is a feature we must *show*, not claim. Remove or demote:
- **Generic "Workflows" and "Test Inputs" as primary navigation** → fold into a model's validation
  plan / rename to "Validation datasets"; they are plumbing, not the product.
- **Generic A/B "Run Comparison" as the headline** → reframe as "Run validation test suite" against
  a governed model. Two-prompt comparison becomes one test type among many, not the identity.
- **Agent trajectory comparison** (already flag-gated) → keep off by default, but **do not delete
  the code**: it's a latent asset for *agentic-AI validation* (§6.10), a real first-mover gap and a
  fast-follow. Just keep it out of the v1 default surface.
- **Any residual "AI workflow QA / experimentation" framing** in copy, SDK docs, and README.
- **The model inventory's ambition to be a system of record** → keep only a scoped, export/sync-
  friendly *AI-model register* (the models we validate), explicitly not the bank's master inventory.

---

## 6. MENA first-mover opportunities (competitive white space)

Where the incumbents are structurally absent and we can be first — most are already wired into the
phases above; this section makes the strategic bet explicit.

1. **Arabic / Gulf-dialect financial-LLM validation (Phase 2).** Arabic evaluation science is
   nascent and academic; no commercial validation vendor productizes it for finance, yet CBUAE
   mandates Arabic explainability. Highest-conviction first-mover.
2. **Sovereign-model validation presets — Jais / Falcon / Allam (Phase 2).** Validate the models
   the region actually deploys; the incumbents only benchmark GPT/Claude/Gemini. Also a natural tie
   to the Abu Dhabi AI ecosystem (TII/G42) that Hub71 sits inside.
3. **Self-hosted as a data-sovereignty compliance capability (cross-cutting).** Position on-prem/
   in-country as the answer to PDPL banking-data localization and CBUAE cloud-access/audit rules —
   turning the incumbents' SaaS into a liability. Structural, hard to copy.
4. **Unified UAE multi-regulator evidence pack (Phase 3).** One platform, CBUAE + DFSA + FSRA + SCA
   + MMS + AI guidance. Nobody offers this local depth.
5. **Bilingual (Arabic/English) regulator- and committee-ready evidence (Phase 1).** Mandated by
   CBUAE, unaddressed by global tools.
6. **Sharia / Islamic-finance model validation (future / optional).** AI in Islamic banking
   (Sharia-compliant product recommendation, contract/Takaful screening) needs Sharia-alignment
   checks no global vendor addresses. Distinctive but needs a domain partner — flag as a fast-follow,
   not v1.
7. **Third-party / vendor AI model due-diligence (Phase 3).** CBUAE is explicit that accountability
   stays with the institution even when the model is a third-party/cloud vendor, and requires vendor
   due diligence. A workflow to validate a vendor LLM you don't control and emit a due-diligence
   evidence pack is directly mandated and under-served locally. High-conviction, CBUAE-anchored.
8. **Model-version-change → re-validation trigger (cross-cutting).** GenAI models change after
   deployment ("models don't stand still" — the SR 26-2 narrative). Detect when a model's underlying
   version/endpoint changes and flag it for re-validation. This is a *targeted* capability, distinct
   from the full production-monitoring pipelines we keep out of scope — and a sharp answer to the
   post-deployment gap incumbents' point-in-time validation misses.
9. **A curated Arabic / GCC financial red-team & adversarial corpus (Phase 2, data moat).** The test
   *content* — maintained Arabic/Gulf jailbreaks, bias probes, hallucination traps, and adverse-action
   cases specific to GCC banking — is itself defensible and hard for incumbents to replicate. Own the
   corpus, not just the harness.
10. **Agentic-AI validation (fast-follow, reuse the existing agent code).** SR 26-2 excludes *agentic*
    AI as well as generative — an even newer, more feared gap (tool-use, guardrails, kill-switch
    behavior) with essentially no validation tooling. We already have trajectory-comparison code to
    repurpose. Not v1, but a strong second wedge.

Recommended first-mover bets for v1: **#1, #2, #3, #5, #9** (Arabic depth + sovereign models +
self-hosted + bilingual evidence + the Arabic red-team corpus as the moat). #4 and #7 land in
Phase 3; #8 is a small high-value add; #6 and #10 are fast-follows.

---

## 7. Cross-cutting

- **Self-hosted first**, single-tenant OSS; `ee/` multi-tenant remains opt-in and out of v1 scope.
- **Additive, backward-compatible migrations** (new `_migration_vN` in `core/store_impl/base.py`;
  latest is v12); verified on SQLite and (CI) PostgreSQL.
- **RBAC + audit** on validation sign-off and report generation; append-only audit log.
- **Testing discipline:** full backend suite + frontend build/tests green at each phase; suite stays
  order-independent.
- **SDK contract preserved** — the governed-model link is an optional addition to "log your outputs."

## 8. Explicitly out of scope

- Being the bank's model inventory system of record, MRM workflow engine, or analyst worklist.
- Executing arbitrary internal/proprietary models inside ForkMark (sandboxed compute) — use the
  SDK-stream path instead.
- Automated production drift-monitoring pipelines (a later "ongoing monitoring" build).
- Traditional (non-AI) model validation — that's the incumbents' and the bank's existing MRM's job.

## 9. Key risks & mitigations (be honest with ourselves)

- **Depth-over-breadth risk.** A "test library" sprawls easily. v1 must do a *few* tests deeply and
  credibly, not many shallowly — a shallow library is exactly the exposure we're trying to avoid.
- **Arabic capability is genuinely hard** for a solo founder to build from scratch. Mitigation: stand
  on existing Arabic-eval work (academic benchmarks like AraDiCE / BALSAM, MBZUAI/G42 ecosystem)
  rather than reinvent it, and line up native-Arabic review for credibility.
- **Self-hosted still needs trust.** On-prem answers data residency but banks will still expect
  security assurance (pen-test, hardening, eventually SOC 2 / ISO 27001) even for software they run
  themselves. Budget for it.
- **Demo access to sovereign models.** Falcon and Jais are open-sourced, so Jais/Falcon validation
  demos are feasible without vendor deals — confirm and pin versions early.
- **Regulatory drift.** CBUAE AI guidance (Feb 2026) is currently *non-binding* and SR 26-2 is new;
  frameworks will move. Keep the regulatory mapping data-driven (as it already is) so packs update
  without code changes.

## 10. Go-to-market wedge (brief, for the Hub71 lens)

Land via a **single UAE design-partner** (a CBUAE-licensed bank or a DIFC/ADGM entity) validating one
real GenAI use-case (e.g., an Arabic customer assistant), possibly through a **DIFC or ADGM regulatory
sandbox**. One credible reference in-market beats breadth. This is a note, not a build item — but it
should shape which demo and which tests we prioritize.

## 11. Decisions needed from you

1. **Confirm the hero framing:** UAE-first, EU/US as options — reflected across product, demos,
   README, and the regulatory pack ordering. (Assumed yes.)
2. **Arabic depth for v1:** how far into dialects for the first cut — MSA + Gulf only, or broader?
3. **Sovereign-model presets:** Jais and Falcon for v1; Allam (Saudi) as fast-follow?
4. **Self-hosted-only for v1**, or keep a managed option on the roadmap despite the localization story?
5. **Sharia validation:** fast-follow with a domain partner, or explicitly park it?

## 12. Recommended sequencing (and the Hub71 lens)

Build **Phase 0 + 1 + the first slice of Phase 2 (Arabic + one hallucination/robustness test +
a Jais/Falcon preset)** as the first milestone. That is enough to *demonstrate depth* — a
self-hosted, Arabic-capable, CBUAE-mapped validation of a sovereign model producing a bilingual
memo — which is the single most persuasive thing we can put in front of Hub71. Breadth (full test
library, all regulator packs, integration surface) follows once the wedge is proven.

Hub71 funds a sharp wedge + demonstrable depth + a credible founder + a clear why-now — not a broad
platform. This plan is deliberately narrow so the depth shows.
