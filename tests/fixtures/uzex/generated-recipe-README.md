# Generated UZEX recipe fixture

`generated-recipe-responses.json.gz` contains sanitized HTTP JSON bodies captured
from UZEX on 2026-10-08 while running
an earlier discovery recipe. The later unactivated discovery variant is retained
at `recipes/archive/uzex-tender-generated.json` (`generated-2026-10-09-2`). It preserves five list pages, 82 detail
responses, and the empty terminal page. The sanitized capture omits product payloads and cannot replay the later variant
unchanged. Retained fields are: listing identity/count/display number/title/buyer/deadline/cost/currency/region/district/type and detail
identity/display number/category/description/buyer/status/deadline/currency/cost. Contacts, addresses, tax IDs, and unrelated fields
were removed. Tests replay these bodies locally; they make no network or AI calls.

SHA-256: `043f9f4fb72c05d71cd7c20723a8f1a298b0587fea92b72417eb9d6cbf253e92`
