# UZTender regression fixtures

- `captured-sample.json`: sanitized observed listing/detail shapes, including missing
  optional products/documents/publication times.
- `recipe.json`: original Codex-authored `codex-2026-10-12-v2` recipe, preserved for
  historical mapping, total reconciliation and fail-closed pagination tests.
- `pilot-100.json`: retired one-page pilot, retained for approval/limit regressions.

Tests use these committed files, never ignored `.codex/recipes/` copies. The current
operator-importable recipe is [`../../../recipes/uztender.json`](../../../recipes/uztender.json).
Fixtures and recipe parsing do not authorize public requests or source activation.
