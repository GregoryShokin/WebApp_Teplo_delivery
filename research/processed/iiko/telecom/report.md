# Telecom Mango Office Handoff

Status: blocked on 2026-05-19 before credential use.

## What Ran

- Created `research/private/mango/raw/`, `research/private/mango/`, `research/processed/iiko/telecom/`, `research/scripts/mango/`.
- Confirmed `research/private/` is ignored by `.gitignore`.
- Saved the anonymous landing response under `research/private/mango/raw/`.

## Auth Contour

Anonymous GET to the LK root returns `302` to Mango's OIDC authorize route on the auth host. The project rules say to stop on OAuth/OIDC redirect, so no login POST was sent and credentials were not used.

## Data Contour

Cached private dashboard JS shows report loading through relative POST endpoints:

- `/product-expenses/report-request`
- `/product-expenses/report-status`
- `/product-expenses/report-result`
- `/product-expenses/report-export`

No GET data endpoint was found. Calling those POST report endpoints would violate the current instruction that only the login form may be POSTed.

## Control And Processed Outputs

April 2026 control was not checked. The expected reference remains `10 189.52`.

`mango_office_monthly.csv` and `telecom_monthly.csv` were not generated, because no Mango monthly amounts were captured.

## Owner Decision Needed

Choose one path before continuing:

- allow a browser/session path for OIDC auth, or
- explicitly classify the report POST endpoints above as read-only report actions and approve their use.
