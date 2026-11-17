# Sanitized central-portal protocol fixtures

Observed anonymously on 2026-10-05 at https://xarid.icppa.uz (central portal).
Origin discovery, scope, live field checks and limitations: [docs/sources/ICPPA.md](../../../docs/sources/ICPPA.md).

- `listing.json`: eleven unique real records selected from a 159-row budget Tender
  sweep, ordered by central lot ID. Ten representative first-page records plus
  real stale-status lot26111006507659 exercise pagination/current detail authority.
- `details.json`: matching real public detail structures. Buyer names, taxpayer
  numbers, organization codes and addresses are replaced consistently. Vendor
  identity/bank-account values are null. Public procurement IDs, product names,
  amounts, dates, status enums and JSON types are preserved.
- `page.json`: observed Spring pagination envelope with explicit lotId descending
  ordering. Content is supplied by the fake adapter. Total159/16pages is reduced
  to11/2pages for deterministic tests; every page metadata field is recomputed.
- `untitled.json`: real lot26111006512875, whose listing names are blank and public
  detail products are empty. Same identity sanitization. Demonstrates external
  absence of central title information rather than a parser regression; verified
  operator object text is now recovered without inventing a title.
- `operators.json`: observed linked GetTrade structures for all eleven baseline
  central records. Preserve display_no, ID, quote/currency, bidding states/dates,
  countdown, attachment metadata and JSON-string budget_products. Buyer INN is
  replaced consistently; embedded products retain public object names/descriptions,
  quantities and units, with account/contact fields removed.
- `enrichment-cases.json`: all nine real central content gaps and their verified
  operator data and observed filtered listing title rows; four additional observed structures cover pending/cancelled null
  dates and USD/EUR quotes. Central buyer names/INNs/codes and vendor identity are
  sanitized; public procurement IDs, product text, amounts/dates/types remain.
- `attachment-protocol.json`: observed anonymous POST download contract and PDF
  response metadata for two lot512875 documents. A minimal sanitized text witness
  confirms object text and bidding dates against the structured API; no full PDF,
  buyer identity, contacts or guessed publication time is included.
- `witness-26111006517270.html`: minimal passive representation of labels and values
  captured from rendered detail text/screenshot, not a verbatim DOM snapshot.
  Buyer is sanitized consistently; real amount76,296,000,000.00UZS independently
  validates API minor-unit conversion7629600000000×0.01. No remote assets/scripts.

No cookies, session state, request headers, credentials, contact details, private
network captures or authenticated data are included. `.env` is never used by tests.
Variants explicitly synthesize errors/drift/duplicates and raw financial/UI changes.
Operator `rest_time` is now observed; synthetic changes test raw countdown volatility.
No view counter was observed.

Fake transports reject unknown URLs. Browser tests serve only owned localhost.
`make live-icppa` is an optional read against a dedicated migrated PostgreSQL target,
excluded from normal CI. Recipe v4 enriches central content gaps through exact public
association checks. Null title is permitted; genuine absence of every content
alternative still fails health. Earlier null-title fixture representation was corrected to the observed blank
string; null and whitespace variants remain explicit regressions. No partial-record lifecycle or relaxed absence rule.
