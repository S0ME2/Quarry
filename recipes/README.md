# Generic recipe examples

`api.example.json` documents declarative cursor exhaustion. Execution bounds belong
to the run, independently of the recipe. It is a template, not an active source.

`uztender.json` is a versioned full-capability replacement for the obsolete
recipe-limited pilot. It preserves captured extraction mappings, follows offset
pages until an observed empty terminal page and declares global inventory metadata.
The same file supports bounded 100/500 and full runs. Activation and live execution
are separate operator actions; offline checks do not establish live readiness. Historical recipes are committed under `tests/fixtures/uztender/`; local originals
under `.codex/recipes/uztender/` remain preserved.
See [operator commands](../docs/operations/VERIFY_UZTENDER.md).

Catalog definitions belong under `sources/<source-id>/`. Retired source contracts
remain under `sources/legacy/`. `archive/uzex-tender-generated.json` preserves an
unactivated discovery variant with identity assertions and raw product objects;
it is not the active catalog recipe. Local discovery exports belong under ignored
`artifacts/recipe-discovery/`. Authoring behavior is documented in
[recipe authoring](../docs/architecture/RECIPE_AUTHORING.md).
