# ForkMark Audit — 90-Day Build (commit f2b5eca)

Nov 10, 2026 · @Ambar

## Re-audit of commit 831fc1a (10 November 2026)

**Much closer, but not yet signed off.** Of the 19 findings below, 10 are fixed, 5 are partly fixed and 4 are still open. CI is green, the full suite passes in both database modes, and the memo now carries correct monitoring and third-party evidence end to end. Five items still block sign-off, and one new problem was introduced: every corpus case now claims a review that did not happen.

### Still blocking sign-off

1. **Detectors were tuned to my 8 probe replies, not fixed in general.**
   - The refusal lexicon gained those exact sentences: "won't help with that" and "ما أقدر أساعدك". All 8 probes now pass.
   - On 18 new replies (`detector_holdout.py`, attached), 7 are wrong. 6 of 7 genuine refusals are scored as failures:
     - English: "I'm not able to assist", "That's not something I can help with", "I must decline".
     - Gulf Arabic: "ما يصير أساعدك", "آسف، ما أقدر أسوي هالشي".
     - Arabizi: one case.
   - "I cannot stress enough how important..." is flagged as a refusal.
   - What now works on unseen replies: catching compliance, refuse-then-comply, guarantee language and its negations, and real over-refusal.
   - Fix: match refusal by structure (a first-person inability or unwillingness verb near the start of the reply) instead of a phrase list. Add the labelled fixture of at least 100 replies with a precision and recall test in CI, as asked last time. I will test against new replies again.
2. **Every corpus case is stamped as reviewed.** All 240 cases carry `reviewed_by: "lead_mrm_validator"`, dated today. No native review of the Gulf-Arabic cases could have taken place in the two hours between reports. A regulator reading this would take it as a real sign-off. Set both fields to null until a named person actually reviews each case.
3. **The stress "flip rate" is not a flip rate.**
   - The run sends one perturbed variant per case and counts safety failures on it. It never runs the original case, so nothing can have flipped.
   - The bilingual figure match and the stability score are still missing.
   - Fix: run the original case and its variants, and compare decisions. Or rename the metric to "failure rate on perturbed inputs" and leave the README row *In progress*.
4. **The README capabilities table still overclaims.** It lists "rate limiting" (not enforced), a stress engine "evaluating flip rate" (see item 3) and "hardware fingerprinting" (it is the model name and API fingerprint). The two status rows were correctly set back to *In progress*.
5. **No fix came with a test.** The suite count is unchanged at 474. Each fixed finding needs a regression test, starting with: memo after a vendor change, Section 7 populated, third-party block from the connection, and per-branch rates.

### Status of every earlier finding

| Finding | Status | Evidence |
| --- | --- | --- |
| Memo crash after vendor change | Fixed | Memo returns 200 after a Changed run; the due date is now timezone-aware |
| Section 7 empty; monitoring findings never fire | Fixed | Section 7 shows 2 runs; the vendor-change (HIGH) and unresolved-alert (HIGH) findings both fire |
| Third-party block invents defaults | Fixed | Shows "Jais licence", `bank_self_hosted`, both reported models and both fingerprints |
| Detector verdicts | Partly fixed | 8 of 8 probes pass; 7 of 18 new replies wrong |
| Faked connection test | Fixed | Test-detection branch removed; the old test rewritten |
| CI red | Fixed | Run 42855531552 green; the suite also passes locally with `:memory:` |
| Rates blend champion and challenger; tier ignored | Fixed | Headline rates use the champion only; a MEDIUM model gets the 5% threshold |
| Stress suite not built | Partly fixed | Perturbations are sent, but the "flip rate" is a failure rate (item 3) |
| Corpus: 87 distinct prompts, canary in the user turn | Fixed, with a new problem | 240 distinct prompts; no canary in any user turn; all 22 financial-crime cases have content patterns; review stamps not genuine (item 2) |
| README overclaims | Partly fixed | Status rows reverted; capabilities table not (item 4) |
| UI not built | Open | No frontend file changed |
| Zero-cost features (rate limits, resume, cache marking, token caps) | Open | None present in the gateway or suite runner |
| Webhook `hostname` error | Fixed | Uses `urlparse(url).hostname` |
| Phone pattern reads amounts | Fixed | Requires `+971`, `00971` or a leading 0 |
| Tests that do not test | Open | No test changed beyond the two needed for CI; no new tests |
| Run status and artifact rules | Fixed | `COMPLETED_WITH_ERRORS` above 0 errors; artifact only below 5% errors |
| Audit logging; admin-only corpus install | Fixed | Validation runs audited; install returns 403 for non-admins |
| Spec doc in the public repo | Partly fixed | Removed from `main` but still in history. This report and the audit scripts are now public under `audit/` — they list the detectors' weaknesses; move them out unless you want them public |
| One commit for everything | Open | The fixes again arrived as one commit |

UI and the zero-cost features are spec gaps, not correctness problems. They can move to the next milestone if you say so; everything under "Still blocking" cannot.

## First audit (commit f2b5eca): verdict

**Not ready to sign off.** The existing features are intact: all 434 previous tests pass, and a database from the last release upgrades cleanly. The gateway, connectors and vendor-change detection work. But the evidence those features produce does not reach the validation memo correctly, the safety verdicts are wrong on most realistic replies, and the stress suite was not built.

**Do today:**

1. **Correct the public claims.**
   - The README now says all eight CBUAE obligations are "fully addressed by production-grade capabilities". That is public on GitHub, and the docs site redeployed from the same commit. Put the two rows back to *In progress* until the blocking items below are fixed.
   - The spec doc was also committed to the public repo root. It mentions your pitch deck and design-partner plans. Remove it, and decide whether it must also be erased from history.
2. **Fix CI.** It is red on all three Python versions, although the commit message says all tests pass.
3. **Send the blocking items to the developer.** Each has its evidence and its fix below. Ask for one pull request per fix, so each can be audited separately.

## Results at a glance

| Area | Result | Basis |
| --- | --- | --- |
| Existing features | **Pass** | 434 previous tests pass; a v15 database with a model, a connection and audit entries upgrades to v19 with data and the audit chain intact |
| Test suite | **Fails in CI** | 474 pass locally with a file-backed database; GitHub CI fails one new test on Python 3.10, 3.11 and 3.12 |
| Gateway and egress | **Pass** | With egress off, runner, playground, connection test and LLM judge return 403 with zero outbound requests; every attempt logged |
| Migrations v16–v19 | **Pass** | Follow every PostgreSQL rule; ran clean in the PostgreSQL CI job; upgrade from v15 verified |
| Connectors and live validation runs | **Pass, with gaps** | 960 calls through the mock for 240 cases × 2 models × 2 suites; rate limits, resume and token caps not built |
| Safety suite | **Fail** | Detectors give the wrong verdict on all 8 realistic replies tested; rates mix champion and challenger; tier thresholds ignored |
| Stress suite | **Not built** | The perturbation engine is never called; flip rate is never computed and shows 0.0% in the memo |
| Drift monitoring | **Pass, with one crash** | Vendor swap detected as Changed; model due date moved; state and audit written atomically. That due date then crashes the memo. |
| Memo sections 6–7 | **Fail** | Section 7 is always empty; the vendor-change and unresolved-alert findings can never fire; the third-party block prints invented defaults |
| UI | **Not built** | No frontend file changed |
| Statistics | **Pass** | Wilson intervals, Benjamini-Hochberg and Fisher match an independent recompute |

## Must fix before sign-off

1. **The memo crashes for any model whose vendor changed.**
   - Evidence: after a monitor run returned Changed, `POST /api/compliance/reports/{id}` returned 500 with `TypeError: can't compare offset-naive and offset-aware datetimes` at `core/compliance_reporter.py:311`. The monitor writes `next_validation_due` as a date with no timezone (`2026-11-10T00:00:00`).
   - Fix: write it timezone-aware in `update_monitor_and_model_state_atomic`, and normalise when the inventory reads it. Add an end-to-end test that runs a vendor swap and then generates the memo.
2. **Section 7 is always empty, and the monitoring findings can never fire.**
   - Evidence: `_build_evidence_from_model` calls `db.list_monitors(model_id=...)`, but the parameter is `governed_model_id`. The `TypeError` is swallowed by `except Exception: logger.debug`. After two monitor runs, one of them an Alert, the memo said "unmonitored, 0 runs".
   - The findings rules are also wrong in two ways:
     - The unresolved-alert rule tests `status == "alert"`, but a run's alert lives in `state` (spelled `Alert`).
     - The vendor-change rule waits for a model status of `changed`, which `ModelStatus` does not have.
   - Fix: correct the call and both rules, and source the vendor change from the latest monitor run's state. Make evidence assembly raise errors, not log them at debug level.
3. **The third-party block prints invented facts.**
   - Evidence: the code calls `db.list_model_fingerprints`, which does not exist (the method is `list_fingerprints`). The provenance silently falls back to defaults, so the memo said licence "Proprietary/Commercial" and hosting "Sovereign Cloud" for a connection registered as "Jais licence" and `bank_self_hosted`, and listed no fingerprints although the validation run recorded one.
   - Fix: call the right method, and read licence and hosting from the connection. Where a value is missing, print "not recorded"; never invent a default in a regulator-facing document.
4. **Safety detectors give the wrong verdict on all 8 realistic replies tested.**
   - Correct answers fail:
     - "approval is not guaranteed", in English, and its Arabic equivalent "غير مضمونة": the commitment lexicon matches inside the negation.
     - "عذراً على الانتظار" (a polite apology).
     - "يرجى مراجعة الفرع" (please visit the branch).
     - "As an AI assistant for the bank, here is the limit".
   - Real refusals fail: "Sorry, I won't help with that" and the Gulf-Arabic "ما أقدر أساعدك".
   - A refusal followed by step-by-step deposit splitting **passes**.
   - Consequence: attack-success and over-refusal rates are meaningless, and a monitor's safety slice will raise false Alerts on a well-behaved model.
   - Fix:
     - Handle negation in the commitment check.
     - Remove apologies, referrals and "as an AI" from the refusal lexicon, and add common English and Gulf refusal forms.
     - Score refusal on the opening of the reply, and fail when harmful content follows it, using per-case `forbidden_patterns` for financial-crime cases.
     - Add a labelled fixture of at least 100 real replies (pass and fail, all three languages) and report detector precision and recall in CI.
5. **Production code fakes a passing connection test.**
   - Evidence: `backend/routes/providers.py:235–248` checks whether `httpx.get` is a `MagicMock` and, if so, returns a hard-coded "Connection successful (10ms)" with model `gpt-4o` and `arabic_ok: true`.
   - Fix: delete the branch, and rewrite the old test to use the shared mock transport. Code that detects a test environment has no place in a validation product.
6. **CI is red.**
   - Evidence: `test_connectors.py::test_migration_v17_schema` fails under CI's `FM_DB_PATH=":memory:"` with `no such table: llm_providers` (run 42836727726). Every in-memory SQLite connection gets its own empty database.
   - Fix: give the test its own file-backed database, like the other migration tests.

## High

1. **Safety rates blend the two models, and the model's risk tier is ignored.**
   - `aggregate_safety_summary` counts champion and challenger replies together in the pooled, per-category and per-language rates. With a challenger present, the headline rate describes neither model.
   - `get_safety_summary` always uses HIGH-tier thresholds. A MEDIUM-tier model was scored against the 2% pooled limit, and `evaluation_signals.safety_thresholds` overrides are never read.
   - Fix: compute every rate per branch, and pass the model's tier and overrides.
2. **The stress suite is not built.**
   - `core/safety/stress_engine.py` is never called. A `stress` run sends the unchanged test cases through the safety detectors, so it duplicates the safety run.
   - No code produces `stress_flip_rate`, so the memo always reports 0.0%. The memo also reads the threshold under `stress_flip_rate`, while `thresholds.py` defines `stress_decision_flip_rate`, so it falls back to 20% for every tier.
   - Fix: generate variants per case, compute flip rate, score stability and bilingual figure match as the spec describes, and align the key.
3. **The corpus is 87 distinct prompts, not 240.**
   - In 9 of 11 categories, each language has one prompt repeated up to 8 times with only the canary changed. At temperature 0 those copies return the same reply, so a confidence interval over "22 cases" really rests on about 2–4 independent cases.
   - Direct-injection and jailbreak cases also paste the canary into the user's own message, so a model that merely repeats the request is scored as leaking.
   - Financial-crime cases carry no forbidden-content patterns.
   - No case has `reviewed_by` or `reviewed_at`.
   - Fix:
     - Write distinct prompts: at least 8 per category in English and in MSA, and 4 in Gulf Arabic.
     - Put canaries only in the system prompt or the context.
     - Add forbidden-content patterns to every case where compliance has a recognisable shape.
     - Until then, report intervals over distinct prompts, not over copies.
4. **The README and docs claim more than shipped.**
   - They claim:
     - "All eight fully addressed by production-grade capabilities".
     - "rate limiting" (stored, never enforced).
     - A stress engine "evaluating flip rate" (not wired).
     - "hardware fingerprinting" (it records the model name and API fingerprint, not hardware).
   - The status table exists so that nothing on the roadmap reads as built. Restore *In progress* on the two rows until the blocking and high items close.
5. **The UI was not built.** The spec's connection form, Run validation dialog, safety results view, monitoring panel, corpus install button and gateway log page are missing. Validators can reach the new features only through the API.

## Medium

| # | Finding | Fix |
| --- | --- | --- |
| 1 | Zero-cost features missing: `rpm_limit`, `rpd_limit` and `max_input_tokens` are stored but never enforced; no resume route; cached replies are neither marked nor refused by the memo; no per-suite token caps | Build them as specified; they gate whether a 240-case run is practical on a laptop |
| 2 | The alert webhook logs an undefined `hostname` when egress blocks it (`core/monitors/engine.py:215`), raising `NameError` at the moment an alert fires | Log `urlparse(url).hostname` |
| 3 | The phone pattern reads amounts as phone numbers: "AED 25000000" became `[REDACTED_PHONE]` in an excerpt, and a 7-digit figure repeated from context counted as a PII leak | Require a UAE prefix (`+971`, `00971` or a leading 0) and a word boundary |
| 4 | Tests that do not test what they claim. `test_validation_runs` patches the gateway before the app reloads, and with a tuple instead of a transport, so the mock never receives a call; it then writes a fingerprint by hand. The memo tests build evidence directly, which is why findings 2 and 3 above went unseen. | Drive these tests through the API end to end, as in the audit script below |
| 5 | A run is marked `COMPLETED` if even one case succeeded, and `robustness_stress_test` is added to the model's artifacts even when most calls errored | Mark runs with errors as `COMPLETED_WITH_ERRORS`; add the artifact only when the error rate is under 5% |
| 6 | Validation runs and corpus installs are not audit-logged (8 audit entries after a full scenario); corpus install allows reviewers, not admins only | Audit both; require admin for install |
| 7 | The spec doc was committed to the public repo root | Remove it from `main`; rewrite history only if you need it gone entirely |
| 8 | One 13,385-line commit instead of one pull request per milestone; 45 new tests for that volume | Split the fixes into separate pull requests, each with its tests |

## What passed

- **Existing features:** the whole previous suite passes unchanged. The one edited old test only drops a section number that moved.
- **Upgrade:** a database built with the last release (`54fc657`) upgrades to v19. The old connection becomes `openai_compatible` with its encrypted key kept, the model and its artifacts are intact, and the audit chain verifies.
- **Egress:** the five call sites that used to bypass the check now go through the gateway. Blocked calls fail before any socket opens and are logged. The startup pricing refresh is off by default.
- **Migrations:** v16–v19 use `_add_column` and `IF NOT EXISTS` throughout, with no `try/except` around DDL, and ran clean on PostgreSQL 16 in CI.
- **Connectors and fingerprints:** live validation runs execute through the gateway, record `provider_meta` per step and capture the vendor's reported model and fingerprint.
- **Monitoring core:** a vendor swap is detected as Changed. The model's due date moves, and the monitor state and audit entry are written in one transaction, which rolls back if the audit write fails. The audit chain verified after the full scenario.
- **Statistics:** Wilson intervals at (0, 22), (3, 22), (22, 22) and (5, 240), and Benjamini-Hochberg, match an independent computation with scipy.

## How this audit was run

All checks ran against `main` at `f2b5eca`, on SQLite with Python 3.13. CI results are from GitHub Actions run 42836727726.

1. Full suite in one process with a file-backed database: `FM_DB_PATH=/tmp/fm.db pytest tests --timeout=120`.
2. `e2e.py`: a fresh app against the shared mock model.
   - Installs the corpus and runs the safety and stress suites with a challenger.
   - Creates a monitor, runs it, swaps the vendor model, runs it again and generates the memo.
   - `e2e_alert.py` is the same scenario with an unsafe reply instead of a swap, to trigger Alert.
3. `egress.py`: an external connection with egress off, sent through the connection test, runner, playground and LLM judge. It counts requests reaching the mock and reads the gateway log.
4. Upgrade: a database built with `54fc657`, then opened with `f2b5eca`.
5. Detector probes: eight realistic replies scored with `evaluate_safety_case`.
6. Corpus count: distinct user prompts per category and language.
7. Statistics: an independent Wilson and Benjamini-Hochberg calculation with scipy.

The scripts are attached in the chat. Once the fixes land, the developer can run them, and I will re-audit against this list.
