"""Validation logic for curated BugSigDB records against the LinkML schema.

Pure data-transformation + validation module: loading instance files, resolving
the packaged schema, and running the LinkML validator. No CLI/UI concerns —
those live in :mod:`bugsigdb_curation.cli`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from linkml.validator import Validator
from linkml.validator.plugins import JsonschemaValidationPlugin

#: Matches the trailing " in /some/json/pointer" suffix that the LinkML
#: jsonschema validation plugin appends to every message, so we can surface it
#: as a separate `path` field instead of only living inside free text.
_PATH_SUFFIX_RE = re.compile(r" in (/\S*)$")


class ValidationInputError(Exception):
    """Raised for usage/IO problems: bad file, unparseable YAML/JSON, unknown
    target class, or an unresolvable schema path.

    Callers (the CLI) should treat this as a usage error (exit code 2), as
    opposed to a `Problem` on a `ValidationResult`, which represents the
    instance itself failing schema validation (exit code 1).
    """


@dataclass(frozen=True, slots=True)
class Problem:
    """A single validation problem reported for one instance."""

    severity: str
    message: str
    instantiates: str | None
    path: str | None = None


@dataclass(frozen=True, slots=True)
class InstanceResult:
    """Validation outcome for one object within one instance file."""

    file: Path
    index: int
    target_class: str
    problems: list[Problem] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.problems


def default_schema_path() -> Path:
    """Resolve the filesystem path to the packaged `bugsigdb.yaml` schema.

    Tries the installed package-data location first (populated at build time
    by hatch's `force-include`, see `pyproject.toml`). Falls back to the
    repo-root `schema/` directory for editable/dev installs, where `uv run`
    points `bugsigdb_curation` straight at `src/` and the wheel-time mapping
    never ran.
    """
    resource = resources.files("bugsigdb_curation").joinpath("data", "bugsigdb.yaml")
    if resource.is_file():
        with resources.as_file(resource) as path:
            return path

    dev_path = Path(__file__).resolve().parent.parent.parent / "schema" / "bugsigdb.yaml"
    if dev_path.is_file():
        return dev_path

    raise FileNotFoundError(
        "Could not locate the packaged bugsigdb.yaml schema (checked package data "
        f"at {resource} and repo-root schema/ at {dev_path})."
    )


def load_instances(path: Path) -> list[Any]:
    """Load an instance file (YAML or JSON) into a list of raw instance objects.

    A file may contain a single object or a list of objects; either form is
    normalized to a list here. Raises `ValidationInputError` for any IO or
    parse problem (missing file, unparseable YAML/JSON, or content that is
    neither an object nor a list of objects).
    """
    if not path.exists():
        raise ValidationInputError(f"File not found: {path}")
    if not path.is_file():
        raise ValidationInputError(f"Not a file: {path}")

    text = path.read_text()
    is_json = path.suffix.lower() == ".json"
    try:
        data = json.loads(text) if is_json else yaml.safe_load(text)
    except json.JSONDecodeError as exc:
        raise ValidationInputError(f"Could not parse {path} as JSON: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ValidationInputError(f"Could not parse {path} as YAML: {exc}") from exc

    if data is None:
        raise ValidationInputError(f"{path} is empty")
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return data
    raise ValidationInputError(
        f"{path} must contain a YAML/JSON object or a list of objects, got {type(data).__name__}"
    )


@lru_cache(maxsize=8)
def _get_validator(schema_path: str) -> Validator:
    """Build (and cache) a `Validator` for a given schema path.

    Constructing a `Validator` compiles the LinkML schema to JSON Schema,
    which isn't free; caching by resolved schema path lets a single CLI
    invocation validating several files reuse one compiled validator.

    Explicitly wires up `JsonschemaValidationPlugin(closed=True)` — the same
    plugin `linkml.validator.validate()` uses by default — since constructing
    `Validator` directly (rather than via that convenience function) runs no
    validation plugins at all otherwise. `closed=True` also flags unknown
    properties, which is useful for catching typo'd field names in curated
    instance data.
    """
    try:
        return Validator(schema_path, validation_plugins=[JsonschemaValidationPlugin(closed=True)])
    except FileNotFoundError as exc:
        raise ValidationInputError(f"Schema file not found: {schema_path}") from exc


def _extract_path(message: str) -> str | None:
    match = _PATH_SUFFIX_RE.search(message)
    return match.group(1) if match else None


def validate_instance(data: dict[str, Any], target_class: str, schema_path: Path | str) -> list[Problem]:
    """Validate a single instance dict against `target_class` in `schema_path`.

    Returns an empty list when the instance is valid. Raises
    `ValidationInputError` for usage problems: an unresolvable schema path or
    an unknown `target_class`.
    """
    validator = _get_validator(str(schema_path))
    try:
        report = validator.validate(data, target_class)
    except ValueError as exc:
        # linkml raises a bare ValueError("No such class: ...") for an unknown
        # target_class; there's nothing more specific to catch.
        raise ValidationInputError(str(exc)) from exc

    return [
        Problem(
            severity=result.severity.value,
            message=result.message,
            instantiates=result.instantiates,
            path=_extract_path(result.message),
        )
        for result in report.results
    ]


def validate_file(path: Path, target_class: str, schema_path: Path | str) -> list[InstanceResult]:
    """Load `path` and validate every instance in it against `target_class`.

    Each top-level object in the file (whether the file holds a single object
    or a list) becomes one `InstanceResult`, indexed in file order.
    """
    instances = load_instances(path)
    results: list[InstanceResult] = []
    for index, instance in enumerate(instances):
        if not isinstance(instance, dict):
            raise ValidationInputError(
                f"{path}: item {index} is not an object (got {type(instance).__name__})"
            )
        problems = validate_instance(instance, target_class, schema_path)
        results.append(InstanceResult(file=path, index=index, target_class=target_class, problems=problems))
    return results
