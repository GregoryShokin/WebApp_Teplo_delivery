# Mango Office Product Expenses Endpoint Map

Last update: 2026-05-20 (headless POST attempt).

## Tenant base

Dashboard URL is read from `MANGO_OFFICE_LK_URL`. Path layout: `/<tenant>/<pbx>/product-expenses`. Tenant/PBX identifiers are intentionally not written here.

## Report API (PHP backend on lk.mango-office.ru)

Form `<form class="communication-costs-filter product-expenses-filter">` posts to relative path `product-expenses/report-request` under the dashboard root. JavaScript module `require/product-expenses` (cached at `research/private/mango/product-expenses.js`) then drives the async pattern:

- POST `/<tenant>/<pbx>/product-expenses/report-request`
  - Body (`application/x-www-form-urlencoded`):
    - `export=` (empty, hidden input)
    - `period=arbitrary` (or one of `today2|yesterday|last_week|last_month|last_quarter|last_year|current_week|current_month|current_quarter|current_year`)
    - `date-range=DD.MM.YYYY - DD.MM.YYYY` (e.g. `01.04.2026 - 30.04.2026`)
    - `grouping=0` (days) or `grouping=1` (months)
    - optional `products[]=<product_id>`
    - optional `services[]=<service_id>`
  - jQuery `ajaxForm` adds `X-Requested-With: XMLHttpRequest`.
  - Response (JSON): `{ status: 'request'|'work'|'complete'|'concurrent-request'|'error', hash, code? }`. When `status === 'complete'`, jump to `report-result` with the same `hash`.

- POST `/<tenant>/<pbx>/product-expenses/report-status`
  - Body: `hash=<hash>&status=1`.
  - Polled every 1s until `response.status === 'complete'` (front-end caps at 120s).

- POST `/<tenant>/<pbx>/product-expenses/report-result`
  - Body: full hash payload echoed back, plus `type=get-report`, `view=product-expenses/records-dataset`.
  - Response (JSON): `{ data: [...], filter: {...}, hash, html_table: '<HTML>' }`. The `html_table` field contains the rendered tree (`pe-tree-l1` row is the product, `data-cost` carries the numeric expense).

- POST `/<tenant>/<pbx>/product-expenses/report-export`
  - Returns CSV (with `X-EXPORT-FILENAME` header) when ready, same async pattern otherwise.

- POST `/<tenant>/<pbx>/product-expenses/expenses`
  - Drilldown for a single date (`date=DD.MM.YYYY&hash=...`). Returns HTML modal contents. Not needed for the monthly total.

Target product row: `Мой продуктовый набор №1` (id `400271344` in our case). Top total lives on the `pe-tree-l1` `<tr>` of that row; do not sum nested `pe-tree-l2/l3` lines.

CSRF: no CSRF tokens in the rendered dashboard HTML. Authentication is enforced solely by PHP session cookie on `lk.mango-office.ru` issued at the end of the OIDC dance.

## Auth contour (auth.mango-office.ru, Angular SPA)

Anonymous `GET https://lk.mango-office.ru/` → `302 https://auth.mango-office.ru/sso/oidc/authorize?...` → `302 https://auth.mango-office.ru/sso/?...&display=password_member,password_account`. The `/sso/?...` page is an Angular SPA (`<app-root>` plus a `<noscript>` reading "Включите Javascript"). There is **no HTML login form**; the form is rendered by `main-*.js` and submits via the Angular HttpClient to JSON-over-`x-www-form-urlencoded` endpoints.

Reverse-engineered endpoints (from `research/private/mango/raw/spa/chunk-TUY22AXI.js`):

| Endpoint                                     | Required body params                                                                | Notes                                  |
|----------------------------------------------|-------------------------------------------------------------------------------------|----------------------------------------|
| `POST /sso/oidc/auth/vpbx`                   | `username`, `password`, `client_id`, `code_challenge`, `captcha_token`, `state`     | + optional `code_challenge_method`. **CAPTCHA REQUIRED.** |
| `POST /sso/oidc/auth/2fa-code`               | `code`, `token`, `client_id`, `code_challenge`, `state`                             | Used after vpbx if 2FA is enabled.     |
| `POST /sso/oidc/auth/account/request-otp`    | `account`, `email`, `client_id`                                                     | Sends OTP to mailbox.                  |
| `POST /sso/oidc/auth/account/verify-otp`     | `account`, `code`, `client_id`, `code_challenge`, `state`                           | Returns `auth_code` on success.        |
| `POST /sso/oidc/auth/member/request-otp`     | `user`, `client_id`                                                                 | Sends OTP to phone.                    |
| `POST /sso/oidc/auth/member/verify-otp`      | `user`, `client_id`, `code`, `code_challenge`, `state`                              | Returns `auth_code` on success.        |
| `POST /sso/oidc/session/start`               | `client_id`, `state`, `redirect_uri`, `auth_code`                                   | Finalises the redirect back to LK.     |
| `POST /sso/oidc/token`                       | `grant_type`, `client_id`, `code`, `code_verifier`, `device_id`, optional secret    | Direct OAuth2 token grant (lk-side).   |

Configuration values shipped in `main-*.js`:

- `smartCaptchaClientKey = ysc1_…826973f` (Yandex SmartCaptcha; production key)
- `isSmartCaptchaTest = false`
- `featureFlags.captcha = true`

OIDC result codes (`chunk-TUY22AXI.js`): `1000` success, `1101 / 1102 / 1107 / 1115` invalid_params, `1103` invalid_token, `1102` (in OTP) incorrect code, `1145` ESIA-link required.

## Headless attempt result (2026-05-20)

The 2026-05-20 headless run reached `/sso/oidc/auth/vpbx`, posted everything that the Angular client wires up except `captcha_token`, and got HTTP 200 with body `{"result":1101}` (= invalid_params). Posting an empty or random `captcha_token` also returns 1101 because the backend validates it against Yandex SmartCaptcha. Without a real browser to solve SmartCaptcha (or a manual OTP delivered to the account owner), the OIDC handshake cannot complete. All login attempts are therefore stopped per the project rules.

Raw artefacts of the unsuccessful run live in `research/private/mango/raw/01_lk_root.*` through `06_session_start.*`. Secrets (cookies, OAuth `code`/`state`/`nonce`, password, captcha tokens) are masked in those files; values were never written in cleartext.
