# Sanitized UZEX protocol fixtures

Observed anonymously on 2026-10-05 at etender.uzex.uz/apietender.uzex.uz.
`listing.json` retains public fields from 21 unique observed lots, including
UZS/USD and null regions. `details.json` retains fields used by the recipe.
Contact details, phones, taxpayer IDs, cookies/session state, headers, document
paths, bid fields and authentication material are omitted. Buyer legal-entity
names, titles/IDs/prices/dates are public procurement data, not authenticated data.

The real sweep had 85 lots. This reduced deterministic fixture adjusts `rn` and
`total_count` to 1–21 / 21, preserving actual field values/types and observed
protocol: inclusive From/To, per-row totals, terminal empty array. Tests explicitly
synthesize drift, partial output, errors, duplicates and new/changed records.

`witness-*.html` holds passive actual DOM excerpts for 513636, 512867 and 508078:
listing title/ID links plus detail buyer/status/dates/amount/currency. Angular
attribute IDs were removed. No scripts, remote assets, forms, contact/session
fields remain. Playwright serves these excerpts and JSON only on owned localhost.

Optional real-site reads use `make live-uzex`; excluded from normal CI.
