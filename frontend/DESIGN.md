# Clearwork implementation

Foundation: [approved Stitch project](https://stitch.withgoogle.com/projects/11549559545742610789), current refined Tenders `8397df20d18443998532a937a9c98dce` and Tender Details `a7a17fa157c4424e912f165c798bcb2b`. Retrieved actual HTML and PNG assets using Stitch MCP; the refinement REVIEW identifies these as newer than final-screens.json.

Four new Stitch screens extend that foundation:

- Sources: `dec152c17768445ba698af85fdd2770e`
- Campaigns: `321021254da24a40b8e45d63bf144a16`
- Matching: `42b6279ffefe4d22ab782b7be3805071`
- Activity: `7232ceedf39e4e39acd82ac20b1dfe2a`

`src/clearwork.css` was extracted from both actual current generated exports; unused selectors and duplicate rules were removed. `src/sections.css` adapts useful structures from each additional export. `src/app.css` supplies real application states and shared controls. All sections use refined cobalt/light tokens, Noto Sans, 176px sidebar, 14px minimum controls, 16px multilingual titles, compact/wide table columns and technical disclosures. Export snapshots and metadata reside in `.codex/frontend-replacement/stitch/` locally; production requires neither Stitch nor a font CDN.

Generated static scripts and demonstration records were discarded. Controls unsupported by actual APIs (daily caps, provider selection, concurrency settings, invented progress percentages) were omitted. Automatic matching uses the backend's rules/semantic modes and lifetime limits. Successful sample validation never claims complete source coverage. React pages consume generated OpenAPI types; all records are backend data.

The former frontend directory was deleted in full before this application was created. No previous React components, styling, UI architecture or state code was reused. Five small section modules load together to keep navigation immediate (lazy loading showed measurable Suspense delay). Query caching comes from TanStack Query, routing from hash URLs (including directly reloadable tender details), dialogs from native HTML, icons from Tabler. After user actions, a shared operation watch keeps completion visible across navigation. Only active operations and pending matching states poll; idle views use cached queries and explicit refresh/focus revalidation.

## Validation

Run `npm ci`, `npm run check`, `npm test`, `npm run build` here. `make api-contract-check` checks generated API types. `uv run --locked pytest tests/e2e/test_web_workspace.py` from project root exercises the built application against isolated PostgreSQL and an owned HTTP source with injected mock AI. No paid requests are needed.
