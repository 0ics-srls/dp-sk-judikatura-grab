"""Phase 78/79 defect: a test module that does path arithmetic at import time (``Path(__file__).parents[4]``) collects on the developer machine and fails inside
the ProcMon test container, where the repo is the only checkout. Two guards: no module-level ``parents[n]`` in tests/, and every test module collects from a
copy of the repo in a temporary directory (nothing next to it)."""
from __future__ import annotations

import ast
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
IGNORED = shutil.ignore_patterns(".venv", ".git", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", "node_modules")


def _module_level_parents(path: Path) -> list[int]:
    """Line numbers of ``.parents`` used outside any function or class body."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[int] = []

    def visit(node: ast.AST, inside_function: bool) -> None:
        if isinstance(node, ast.Attribute) and node.attr == "parents" and not inside_function:
            found.append(node.lineno)
        nested = inside_function or isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
        for child in ast.iter_child_nodes(node):
            visit(child, nested)

    visit(tree, inside_function=False)
    return found


@pytest.mark.unit
def test_no_test_module_does_path_arithmetic_with_parents_at_import_time() -> None:
    offenders = {str(path.relative_to(ROOT)): lines for path in sorted((ROOT / "tests").rglob("*.py")) if (lines := _module_level_parents(path))}

    assert offenders == {}


@pytest.mark.unit
def test_every_test_module_collects_from_a_copy_of_the_repo_in_a_temp_directory(tmp_path: Path) -> None:
    copy = tmp_path / "isolated-checkout"
    shutil.copytree(ROOT, copy, ignore=IGNORED)

    done = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "--deselect", "tests/test_collectable.py"],
        cwd=copy, capture_output=True, text=True, timeout=300, check=False, shell=False,
    )

    assert done.returncode == 0, done.stdout[-1500:] + done.stderr[-500:]


@pytest.mark.unit
def test_the_scanner_flags_module_level_parents_and_ignores_function_level_use(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("from pathlib import Path\nBASE = Path(__file__).parents[4]\n", encoding="utf-8")
    good = tmp_path / "good.py"
    good.write_text("from pathlib import Path\n\n\ndef base() -> Path:\n    return Path(__file__).parents[4]\n", encoding="utf-8")

    assert _module_level_parents(bad) == [2]
    assert _module_level_parents(good) == []
