"""The data-firewall guard test (workflow plan §6e, ledger L013 -- non-negotiable).

The curator must never import `bugsigdb_curation.eval` (or any gold loader),
never read a relational gold CSV, and never read the cached
`data/eval/pmid_pmcid_map.csv`. This is checked two ways:

1. **Source-level scan**: every `.py` file under `src/bugsigdb_curation/curator/`
   is scanned for the literal forbidden import strings and for gold-path
   literals -- a static check that survives even if a forbidden import is
   never actually *exercised* by any test.
2. **Import-time module-graph check**: after importing every curator
   submodule, `bugsigdb_curation.eval` must not appear in `sys.modules`
   unless something *other* than the curator package put it there (this
   process's own eval tests may have already imported it -- see the
   docstring on `test_eval_package_not_pulled_in_by_curator_alone` for how
   that's handled by running the check in a subprocess).
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

CURATOR_DIR = Path(__file__).parent.parent / "src" / "bugsigdb_curation" / "curator"

#: Gold-only file/dir literals that must never appear in curator source, per
#: §6e: the relational export dir and the pmc-map's cached gold-derived CSV.
#: Deliberately exact/unambiguous full paths (not a bare substring like
#: "taxa.csv") since curator docstrings legitimately *describe* the firewall
#: by naming what eval.taxonomy uses and why curator.taxonomy doesn't --
#: `test_ncbi_taxonomy_resolver_has_no_gold_seed_parameter` below checks that
#: boundary precisely instead (by constructor signature, not prose-matching).
_FORBIDDEN_PATH_SUBSTRINGS = (
    "data/exports/relational",
    "data/eval/pmid_pmcid_map.csv",
)


def _curator_source_files() -> list[Path]:
    return sorted(CURATOR_DIR.rglob("*.py"))


def test_curator_package_exists_and_has_modules():
    files = _curator_source_files()
    assert len(files) >= 8, f"expected the curator package to have several modules, found: {files}"


@pytest.mark.parametrize("path", _curator_source_files(), ids=lambda p: p.name)
def test_curator_module_does_not_import_eval_package(path: Path):
    """AST-level check: no `import bugsigdb_curation.eval`, no `from
    bugsigdb_curation import eval` / `from bugsigdb_curation.eval import ...`,
    anywhere in this file -- more robust than a substring grep against
    comments/docstrings that legitimately *mention* the eval package (as
    several curator docstrings do, to explain the firewall itself)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.split(".")[:2] == ["bugsigdb_curation", "eval"], (
                    f"{path}: forbidden import {alias.name!r} (data firewall §6e)"
                )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "bugsigdb_curation":
                imported_names = {alias.name for alias in node.names}
                assert "eval" not in imported_names, (
                    f"{path}: forbidden `from bugsigdb_curation import eval` (data firewall §6e)"
                )
            else:
                assert not module.split(".")[:2] == ["bugsigdb_curation", "eval"], (
                    f"{path}: forbidden `from {module} import ...` (data firewall §6e)"
                )


def _path_like_literals_used_in_code(tree: ast.AST) -> list[str]:
    """String literals passed to a `Path(...)` or `open(...)` call -- i.e.
    actual file-path *usage*, as opposed to a docstring/comment that merely
    *names* a gold path in prose to explain the firewall (several curator
    docstrings do exactly that, deliberately -- both are AST string
    constants, but only this subset represents code that would touch disk).
    """
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        func_name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if func_name not in ("Path", "open"):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found.append(arg.value)
    return found


@pytest.mark.parametrize("path", _curator_source_files(), ids=lambda p: p.name)
def test_curator_module_does_not_open_gold_paths_in_code(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for literal in _path_like_literals_used_in_code(tree):
        for forbidden in _FORBIDDEN_PATH_SUBSTRINGS:
            assert forbidden not in literal, (
                f"{path}: opens forbidden gold-path literal {literal!r} (data firewall §6e)"
            )


def test_curate_takes_no_gold_path_argument():
    """The public entrypoint's signature carries no gold-path parameter."""
    import inspect

    from bugsigdb_curation.curator.pipeline import curate, curate_async

    for fn in (curate, curate_async):
        params = set(inspect.signature(fn).parameters)
        for forbidden_name in ("gold", "relational", "relational_dir", "pmc_map", "pmc_map_csv"):
            assert forbidden_name not in params, f"{fn.__name__} must not accept a {forbidden_name!r} argument"


def test_ncbi_taxonomy_resolver_has_no_gold_seed_parameter():
    """S6's resolver must be constructible from ONLY the general NCBI authority
    + its own cache file -- no `taxa_csv`/`seed` constructor parameter of any
    kind (unlike `eval.taxonomy.TaxonomyResolver`, which seeds from gold)."""
    import inspect

    from bugsigdb_curation.curator.taxonomy import NcbiTaxonomyResolver

    params = set(inspect.signature(NcbiTaxonomyResolver.__init__).parameters) | set(
        inspect.signature(NcbiTaxonomyResolver.load).parameters
    )
    for forbidden_name in ("taxa_csv", "seed", "gold", "relational"):
        assert forbidden_name not in params


def test_eval_package_not_pulled_in_by_importing_curator_alone():
    """In a fresh interpreter, importing the curator package must not import
    `bugsigdb_curation.eval` as a side effect.

    Run in a subprocess (rather than checking `sys.modules` in this test
    process) because other tests in the same pytest session may have already
    imported `bugsigdb_curation.eval` for unrelated reasons, which would
    make an in-process check meaningless.
    """
    script = (
        "import sys\n"
        "import bugsigdb_curation.curator.pipeline\n"
        "assert 'bugsigdb_curation.eval' not in sys.modules, "
        "'bugsigdb_curation.eval was imported as a side effect of importing the curator package'\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert result.stdout.strip() == "OK"
