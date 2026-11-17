"""Repair safety primitives: revocable browser access and validated activation policy."""

from threading import Event
from typing import Any

from tender_system.core.configuration import SourceSettings
from tender_system.core.domain import (
    FixResult,
    Reason,
    RepairActivationMode,
    RepairValidation,
    SourceFailure,
    SourceRecipe,
)
from tender_system.core.ports import (
    BrowserAdapter,
)


class GuardedBrowser:
    """Revocable browser capability; constructed/used/closed on the same owning thread."""

    def __init__(
        self,
        browser: BrowserAdapter,
        failure: SourceFailure,
        settings: SourceSettings,
        revoked: Event,
    ) -> None:
        self.browser, self.failure, self.settings, self.revoked = (
            browser,
            failure,
            settings,
            revoked,
        )
        self.actions = 0
        self.authorized_sessions = (
            {failure.authorized_session_reference}
            if failure.authorized_session_reference
            else set()
        )

    def __getattr__(self, name: str) -> Any:
        allowed = {
            "navigate",
            "click",
            "type_text",
            "select",
            "scroll",
            "content",
            "network",
            "screenshot",
            "trace",
            "download",
            "restore_session",
            "save_session",
            "session_state",
            "wait_for_selector",
            "wait_for_response",
        }
        if name not in allowed:
            raise AttributeError(name)

        def invoke(*args: Any, **kwargs: Any) -> Any:
            if self.revoked.is_set():
                raise TimeoutError("browser investigation expired")
            self.actions += 1
            if self.actions > self.settings.investigation.max_actions:
                raise ValueError("browser action budget exhausted")
            if name == "session_state" and (
                not self.settings.investigation.allow_session_inspection
                or not self.authorized_sessions
            ):
                raise PermissionError("session inspection not permitted")
            if name == "save_session" and not self.settings.investigation.allow_session_changes:
                raise PermissionError("session changes not permitted")
            if name == "restore_session" and (
                len(args) != 1 or args[0] not in self.authorized_sessions
            ):
                raise PermissionError("session reference not authorized")
            if (
                name == "network"
                and (args == (True,) or kwargs.get("include_request_details"))
                and not self.settings.investigation.allow_network_request_details
            ):
                raise PermissionError("request detail inspection not permitted")
            result = getattr(self.browser, name)(*args, **kwargs)
            if name == "save_session" and isinstance(result, str):
                self.authorized_sessions.add(result)
            return result

        return invoke


def change_categories(old: SourceRecipe, new: SourceRecipe, proposal: FixResult) -> set[str]:
    changes: set[str] = set()

    def compare(left: Any, right: Any, context: str = "inventory") -> None:
        if left == right:
            return
        if isinstance(left, dict) and isinstance(right, dict):
            kind = right.get("type", left.get("type"))
            if kind is not None:
                context = {
                    "http_request": "acquisition",
                    "browser_action": "navigation",
                    "extract": "mapping",
                    "transform": "transforms",
                    "custom": "custom_code",
                    "loop": "pagination",
                    "condition": "navigation",
                }.get(kind, "inventory")
            for key in left.keys() | right.keys():
                if key == "version":
                    continue
                category = "selectors" if key in {"selector", "selectors"} else context
                if key in {"session_reference", "authorized_session_reference"}:
                    category = "authentication"
                compare(left.get(key), right.get(key), category)
        elif isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
            for a, b in zip(left, right, strict=True):
                compare(a, b, context)
        else:
            changes.add(context)

    compare(old.model_dump(mode="json"), new.model_dump(mode="json"))
    if proposal.custom_code_reference or proposal.plugin_changes:
        changes.add("custom_code")
    return changes


def activation_decision(
    failure: SourceFailure,
    proposal: FixResult,
    validation: RepairValidation,
    settings: SourceSettings,
    human: bool,
) -> tuple[str, tuple[Reason, ...]]:
    if not validation.passed:
        return "rejected", (
            Reason(code="validation_rejected", detail="Independent validation failed"),
        )
    if human:
        return "activated", (
            Reason(
                code="human_approval_validated",
                detail="Explicit human approval after fresh independent validation",
            ),
        )
    if (
        proposal.confidence < settings.repair.minimum_confidence
        or validation.confidence < settings.repair.minimum_confidence
    ):
        return "awaiting_approval", (
            Reason(
                code="confidence_below_policy",
                detail="Confidence insufficient for automatic activation",
            ),
        )
    if settings.repair.mode == RepairActivationMode.HUMAN:
        return "awaiting_approval", (
            Reason(
                code="human_approval_required", detail="Source activation policy requires approval"
            ),
        )
    if (
        proposal.plugin_changes or proposal.custom_code_reference
    ) and settings.repair.generated_code_requires_approval:
        return "awaiting_approval", (
            Reason(
                code="code_approval_required",
                detail="Generated-code approval required by source policy",
            ),
        )
    assert proposal.updated_recipe is not None
    if settings.repair.mode == RepairActivationMode.LIMITED and not change_categories(
        failure.recipe, proposal.updated_recipe, proposal
    ) <= set(settings.repair.limited_allowed_changes):
        return "awaiting_approval", (
            Reason(
                code="change_outside_automatic_policy",
                detail="Changes exceed limited automatic policy",
            ),
        )
    return "activated", (
        Reason(
            code="validated_automatic_activation",
            detail="Independent validation and source policy authorize activation",
        ),
    )
