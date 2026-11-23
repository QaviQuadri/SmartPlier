# Smartplier architecture

The React dashboard uses same-origin JSON endpoints served through Python's standard-library HTTP server. The CLI invokes the same services. The application runs locally, with a loopback listener and a single-worker queue for background jobs.

## Modules

| Area | Modules | Responsibility |
| --- | --- | --- |
| Entry points | `jobhunt`, `launch`, `demo` | Console tasks, local launcher, and an isolated fictional workspace |
| Discovery | `discovery`, `discovery_sources`, `discovery_mail` | Public sources, read-only Gmail alerts, normalization, and deduplication |
| Eligibility and evidence | `scoring`, `resume`, `resume_templates`, `outreach` | Fit assessment, evidence validation, local document rendering, and outreach drafts |
| Application planning | `ats_detection`, `freshness`, `form_engine`, `form_recipes`, `screening` | Listing checks, form structure, cached classifications, and locally verified answers |
| Application execution | `generic_application`, `auto_apply`, `confirmation_mail`, `prefill` | Guarded browser, submission journal, receipt checks, and legacy review-only prefill |
| Persistence and reporting | `tracker`, `analytics` | SQLite relationships, history, and reports |
| HTTP and background work | `dashboard`, `dashboard_views`, `web_app`, `web_service` | Loopback routing, HTML/JSON presentation, and scheduling |
| Credential lookup | `credentials` | Environment lookup and optional Windows Credential Manager |

## Data model

The JSON profile is the local source of candidate evidence. Experience and project bullets use stable IDs and stories refer to those IDs. The discovery configuration supplies an initial source list.

- `jobs` and `status_history` hold tracked postings and decisions. `job_discovery` and `discovery_aliases` connect postings to normalized sources and original IDs.
- `applications`, `outreach`, and `resume_cache` record outcomes, draft activity, and tailored document selections.
- `screening_questions`, `screening_question_jobs`, and `screening_answers` link unresolved questions and verified answers to affected jobs.
- `form_recipes` stores layout fingerprints, validated canonical mappings, and recipe results.
- `auto_applications`, `auto_events`, `auto_preflight`, `auto_attempt_log`, and `auto_diagnostics` track preparation, reservation, attempt evidence, confirmation, and review.
- `web_settings`, `web_sync`, and `web_gmail_seen` hold dashboard settings, run history, and incremental mail sync metadata.

## Submission boundary

Browser inspection prepares a payload snapshot without sending the application. The payload is checked again against preflight before a SQLite transaction revalidates settings, limits, eligibility, freshness, and duplicates. The transaction reserves the attempt before the browser can send the payload. The network guard permits one submission to the approved origin and handles redirects without replaying it. A confirmation page produces a probable result. A matching Gmail receipt or manual confirmation can promote it to Applied.

If the process fails after reservation, subsequent runs hold the job for review. The attempt counts against the daily cap even if its outcome remains uncertain. A circuit breaker can pause scheduled submissions after repeated bad outcomes or policy blocks.

## Security and data handling

- The HTTP server binds to loopback. It checks Host, Origin, request media type, and CSRF tokens. The content security policy restricts dashboard resources.
- Employer content is treated as untrusted. Browser requests are confined to the approved HTTPS origin and public address. Unsupported actions, access checks, and cross-origin forms require manual review.
- Gemini recipe planning uses sanitized labels, field types, and choices. The answer bank, applicant contact values, and resume attachment are resolved on the local machine. Other Gemini features receive the evidence needed for their task.
- Secrets are read from the process environment or, on Windows, Credential Manager. The application does not load secrets from `.env` files. Gmail access is read-only.
- Saved payload and browser evidence can contain private applicant data. They belong to the local database and must be kept out of publication. Hidden secrets and tokens are redacted from the payload view.
- The demo has an isolated temporary workspace, a mock provider, invented data, and disabled external discovery and submission controls.
