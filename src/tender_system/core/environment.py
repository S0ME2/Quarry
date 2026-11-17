"""Project dotenv, typed process settings, and secret-safe configuration errors."""

import os
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal, cast

from pydantic import BaseModel, Field, ValidationError
from pydantic_core import InitErrorDetails

PROJECT_ROOT = next(
    (
        parent
        for parent in Path(__file__).resolve().parents
        if (parent / "pyproject.toml").is_file()
    ),
    Path.cwd(),
)


def read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for number, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip().removeprefix("export ")
        if not line or line.startswith("#"):
            continue
        key, separator, raw = line.partition("=")
        try:
            parts = shlex.split(raw, comments=True)
            if not separator or not key.strip().isidentifier() or len(parts) > 1:
                raise ValueError
        except ValueError:
            raise ValueError(f"Invalid dotenv assignment at {path.name}:{number}") from None
        values[key.strip()] = parts[0] if parts else ""
    return values


def load_environment(path: Path | None = None) -> Mapping[str, str]:
    for key, value in read_dotenv(path or PROJECT_ROOT / ".env").items():
        os.environ.setdefault(key, value)
    return os.environ


def configuration_error(error: ValidationError) -> ValidationError:
    # Preserve validation exception compatibility without carrying secret input values.
    details = error.errors(include_input=False)
    return ValidationError.from_exception_data(
        "Quarry configuration", cast(list[InitErrorDetails], details), hide_input=True
    )


class StartupSettings(BaseModel):
    model_config = {"extra": "forbid"}
    source_catalog: Path = PROJECT_ROOT / "sources"
    register_catalog: bool = False
    api_host: Literal["127.0.0.1", "::1", "localhost"] = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)
    web_port: int = Field(default=5173, ge=1, le=65535)


class RuntimeSettings(BaseModel):
    """Defaults preserve explicitly composed applications; dotenv loader opts out locally."""

    model_config = {"extra": "forbid"}
    manual_only: bool = False
    operator_worker_enabled: bool = True
    matching_enabled: bool = True
    ai_semantic_enabled: bool = True
    worker_poll_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 1
    operation_timeout_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 900
    browser_timeout_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 30
    browser_enabled: bool = True
    evidence_directory: Path = Path.home() / ".local/share/quarry/evidence"
    session_directory: Path = Path.home() / ".local/share/quarry/sessions"


SAFE_DEFAULTS: dict[str, object] = {
    "manual_only": True,
    "operator_worker_enabled": False,
    "matching_enabled": True,
    "ai_semantic_enabled": False,
}


def startup_settings(environ: Mapping[str, str] | None = None) -> StartupSettings:
    environment = load_environment() if environ is None else environ
    values = {
        name: environment[key]
        for name in StartupSettings.model_fields
        if (key := "QUARRY_" + name.upper()) in environment and environment[key] != ""
    }
    try:
        return StartupSettings.model_validate(values)
    except ValidationError as error:
        raise configuration_error(error) from None
