# Source catalog

`uzex-tender/` contains the maintained UZEX catalog definition, recipe and policy.
Catalog registration is explicit (`QUARRY_REGISTER_CATALOG=true`); ordinary startup
leaves registration off. Registering a catalog is trusted bootstrap provisioning,
not the independent approval workflow for an externally authored recipe.

`legacy/uzex/` and `legacy/icppa/` retain earlier source definitions for reproducible
history and offline protocol/enrichment tests. They keep their original source IDs
and recipe versions. They are nested below the catalog's one-directory discovery
depth and are not automatically registered by startup.

The UZTender operator-importable recipe is [`../recipes/uztender.json`](../recipes/uztender.json).
Import, scoped validation and explicit approval precede collection; see
[recipe workflow](../docs/operations/RELIABILITY_WORKFLOW.md).
