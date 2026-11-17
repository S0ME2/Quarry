# Development tools

`matching/ranking_probe.py` builds and validates experimental batch ranking requests.
It makes no provider calls and is separate from production pairwise matching.
Its contract tests live in `tests/unit/test_ranking_probe.py`.

Maintained application tools retain their installed/module entry points:

| Task | Command |
| --- | --- |
| Offline synthetic matcher replay | `make matcher-benchmark` |
| Offline collected-tender replay | `make real-matcher-benchmark` |
| Historical matcher v1 replay | See [benchmark instructions](../benchmarks/matcher/README.md) |
| Schema drift in an isolated test schema | `make schema-check` |
| Recipe/schema inspection and manual operations | `uv run --locked quarry-manual --help` |
| Source/run health | `uv run --locked tender-health --help` |
| Verified database rebuild | `uv run --locked python -m tools.database_rebuild --help` |
| Isolated database query workload | `uv run --locked python -m tools.database_workload --help` |

Live commands are explicit operator actions; normal checks use offline fixtures.
Generated reports, captures and temporary debugging scripts belong below `.codex/`
or ignored `artifacts/`. Reusable tools belong here and must not contain credentials.
