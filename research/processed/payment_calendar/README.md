# Payment Calendar Snapshot Notes

- Google Sheets ID: `1zfnpFbpeUnNn7wAKx9qoyTMaqYDXIvunWCWWQAFyBWg`.
- All tab gids were parsed from the main HTML page via `bootstrapData` / `21350203` sheet metadata records.
- Raw CSV exports were downloaded to `/private/tmp/payment_calendar_raw/` and were not committed into the repository.
- Processed CSV files in this directory are trimmed to non-empty used ranges and PII-filtered.
- PII filtering: counterparty/supplier list cells, person/responsible cells, private comments/purposes, URLs, emails, phone/account/tax-like long numeric IDs are replaced with stable salted hashes.
- Dates, week numbers, article names, movement type, plan/fact labels, amounts and non-private methodology text are preserved for discovery.
- Formula inspection used the read-only XLSX export because CSV export contains calculated values, not formulas.
