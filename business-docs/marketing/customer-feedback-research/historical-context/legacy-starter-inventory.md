# Starter feedback: artifact inventory (read-only, 2026-10-13)

> Исторический контекст до свежей выгрузки Starter. На 13.10.2026 все 10 535 записи Starter сохранены; актуальный статус и пути — в основном README раздела Customer Feedback Research.

## Finding

No primary Starter customer-review array/export was found in the main checkout, the 26 registered repository worktrees, or the targeted `/tmp` and `/private/tmp` filename search. In `main`, `research/processed/starter/` does not exist, and `research/processed/starter/feedbacks_themes.md` is neither present nor tracked. No matching Starter feedback/review file was found under the checked `research/processed`, `research/raw`, `integrations`, `scripts`, or marketing-doc locations in the worktrees.

The legacy review figures are therefore secondary summaries, not a recoverable source array. The repository graph query also did not surface a Starter feedback node; its results were source-registry and Starter revenue/commission material. The graph is useful for navigation but is not an index of this review corpus.

## Existing references and summaries

- `business-docs/marketing/diagnostic.md:286` — says the 10,136 Starter review corpus had mean rating 4.62 and 13% negative, with speed/cold 43% of negative, taste about 23%, staff 10%, fish/salmon 2.3%, price 0.7%. The note links to the missing `research/processed/starter/feedbacks_themes.md`. No primary rows, period, filters, or coding method are recoverable from this reference.
- `business-docs/marketing/data-channels.md:40-47` — old Starter source registry; labels the `feedbacks`/`feedback/total` route as available and points to `research/processed/starter/`, but the directory is absent. It records 282 OpenAPI paths, superseded by the later 11 October schema count.
- `business-docs/marketing/action-plan.md:43-46` and `business-docs/marketing/roadmap.md:94` — repeat the historical 10,136-review conclusion about speed and salmon. These are derived decisions, not raw review data.
- `/Users/grigorii/Developer/Projects/Teplo-agent-unit-economics/business-docs/marketing/customer-research/2026-10-08/own-review-evidence.md` — dated read-only audit (16,351 bytes). It explicitly says no new Starter export was performed and no fresh row-level own reviews were found in the checked local artifacts. It records the old figures as unverified historical summaries, not a new count; see §§ “Почему старые проценты…” and “Конкретный маршрут обновления Starter.”
- `/Users/grigorii/Developer/Projects/Teplo-agent-unit-economics/business-docs/marketing/customer-research/2026-10-13/feedback-core.md` — broader customer-feedback synthesis (22,807 bytes). It carries the old Starter percentages only as historical context and states their source rows, period, filters, and coding method were not recovered; see lines 7-15, 70-76. It is not the Starter primary array.

## API route documented, not yet proven for review data

The 8 October audit records these routes from the public OpenAPI contract (not from a successful read of review records):

- `GET /api/v1/frontend/feedbacks` — `FeedbackFrontend` array; `offset`, `limit` (default 50), `order_by`.
- `GET /api/v1/frontend/feedback/total` — `total`, `averageRating`.
- `GET /api/v1/frontend/feedback/{feedback_id}` — record detail.
- `GET /api/v1/frontend/feedback/export` — export route; documented permission `export-feedbacks:view`; response format was unspecified in the saved contract extract.

The tenant base in the documented contract is `https://crm.starterapp.ru/fmteplo`. The historical OpenAPI snapshot at `/private/tmp/teplo-promotions-20261007/starter-openapi-public.json` is no longer present. The current local skill `/Users/grigorii/.codex/skills/teplo-customer-analysis/references/starter-sources.md` records a fresh public schema check and successful programmatic login plus one-row customer/order reads on 2026-10-11; those checks did **not** read feedback records or prove the export permission. `/Users/grigorii/.codex/skills/teplo-connections/SKILL.md` documents the cookie-jar login flow, but contains no evidence that the feedback route was called.

## Why the old review material was missed

The confirmed cause is a missing saved export: the 8 October audit explicitly says no new Starter export was run, while the referenced processed file and folder are absent. The marketing source registry's old “available/10,136” status made the analysis look locally recoverable even though it only linked to an endpoint and an absent artifact. A previous browser attempt reached login and timed out (recorded in the prior customer-analysis run); later, on 11 October, programmatic authentication and customer reads succeeded. Thus the evidence supports “not exported/preserved and not indexed,” not “Starter has no reviews” or “the API cannot serve them.” Feedback API access remains to be tested separately by the parent; no credentials were read and no production request was made for this inventory.

## Search scope

Read-only: `git worktree list`; checked targeted review-related paths and filenames in 26 registered worktrees; checked the named research, integration, script and business-marketing locations; searched `/tmp` and `/private/tmp` by likely Starter/review filenames. Excluded credential files, environment files, rollout logs and large logs. The root checkout is `main`; no repository files were modified.

## Legacy repository and Git history check (2026-10-13)

The previous checkout `/Users/grigorii/Documents/Projects/Teplo-all-for-business` exists, but its exact `research/processed/starter/` directory is missing; consequently no `feedbacks*` file exists at that path. `git log --all` in the current repository returned no commits for `research/processed/starter/feedbacks*` (including the recursive glob pathspec). This supports the documented explanation that the June source/analysis files were never committed and were not brought over into the July 29 reconstruction. No other historical directories or session logs were scanned.
