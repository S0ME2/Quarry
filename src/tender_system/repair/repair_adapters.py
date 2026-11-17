"""Recipe executors with instance-local, explicitly registered plugin artifacts."""

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from tender_system.collection.performance import timed
from tender_system.collection.recipes import RecipeExecutor, transforms
from tender_system.collection.source_runs import (
    RunReceipt,
    RunStore,
    SourceRunService,
    request_digest,
)
from tender_system.core.configuration import SourceSettings
from tender_system.core.domain import ExecutionLimits, RecipeRevision, SourceRecipe, SourceRecord
from tender_system.core.ports import (
    BrowserAdapter,
    Clock,
    EvidenceStore,
    HttpAdapter,
    RecipeCatalog,
    SourceExecutor,
    TenderNormalizer,
    Transform,
)
from tender_system.core.registry import Registry


@dataclass(frozen=True)
class StagedPlugin:
    name: str
    sha256: str
    implementation: Transform
    kind: str = "custom"


class RecipeRepairExecutors:
    def __init__(
        self,
        http_factory: Callable[[], HttpAdapter],
        browser_factory: Callable[[], BrowserAdapter],
        evidence: EvidenceStore,
        clock: Clock,
        artifacts: Registry[StagedPlugin] | None = None,
        plugins: Registry[Transform] | None = None,
        public_http: HttpAdapter | None = None,
        public_browsers: Callable[[], BrowserAdapter] | None = None,
    ) -> None:
        self.http_factory, self.browser_factory, self.evidence, self.clock = (
            http_factory,
            browser_factory,
            evidence,
            clock,
        )
        self.artifacts, self.plugins = artifacts or Registry(), plugins or Registry()
        self.public_http, self.public_browsers = public_http, public_browsers

    def __call__(self, revision: RecipeRevision, settings: SourceSettings) -> RecipeExecutor:
        replacements = {}
        for change in revision.plugin_changes:
            artifact = self.artifacts.get(change.artifact_reference)
            if artifact.name != change.name or artifact.sha256 != change.sha256:
                raise ValueError("plugin artifact identity/hash differs from validated proposal")
            if change.name in replacements:
                raise ValueError("duplicate proposed plugin name")
            replacements[change.name] = artifact
        isolated: Registry[Transform] = Registry()
        transformations = transforms(settings)
        for name in self.plugins.names():
            if name not in replacements:
                isolated.register(name, self.plugins.get(name))
        for name, artifact in replacements.items():
            if artifact.kind == "custom":
                isolated.register(name, artifact.implementation)
            elif artifact.kind == "transform":
                # Rebuild registry; registered changes never mutate shared implementations.
                replaced: Registry[Transform] = Registry()
                for key in transformations.names():
                    if key != name:
                        replaced.register(key, transformations.get(key))
                replaced.register(name, artifact.implementation)
                transformations = replaced
            else:
                raise ValueError("unsupported registered artifact kind")
        return RecipeExecutor(
            self.public_http
            if settings.public_readonly and self.public_http
            else self.http_factory(),
            self.evidence,
            settings,
            browser_factory=self.public_browsers
            if settings.public_readonly and self.public_browsers
            else self.browser_factory,
            plugins=isolated,
            transformations=transformations,
            clock=self.clock,
        )


class CatalogSourceRunner:
    """Resolve the activated revision at run start; publication fences in-flight old recipes."""

    def __init__(
        self,
        catalog: RecipeCatalog,
        executors: Callable[[RecipeRevision, SourceSettings], SourceExecutor],
        normalizer: TenderNormalizer,
        store: "RunStore",
        clock: Clock,
    ) -> None:
        self.catalog, self.executors, self.normalizer = catalog, executors, normalizer
        self.store, self.clock = store, clock

    def run(
        self,
        run_id: UUID,
        initial: SourceRecipe,
        source: SourceRecord,
        settings: SourceSettings,
        limits: ExecutionLimits | None = None,
    ) -> "RunReceipt":
        if initial.source_id != source.id:
            raise ValueError("initial recipe source differs from registered source")
        prior = self.store.receipt(run_id)
        if prior is not None:
            # A completed batch pins its recipe even after another repair activates.
            recorded = (
                RecipeRevision(recipe=initial)
                if initial.version == prior.recipe_version
                else self.catalog.revision(source.id, prior.recipe_version)
            )
            if (
                prior.source_id != source.id
                or recorded is None
                or prior.request_hash != request_digest(recorded.recipe, settings, limits)
            ):
                raise ValueError("run ID reused with different source/recipe/configuration")
            return prior
        revision = self.catalog.active(source.id) or RecipeRevision(recipe=initial)
        with timed("executor_initialization", "collection executor initialization"):
            executor = self.executors(revision, settings)
        return SourceRunService(executor, self.normalizer, self.store, self.clock).run(
            run_id, revision.recipe, source, settings, limits
        )
