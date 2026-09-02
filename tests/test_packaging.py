"""Release metadata that is easy to get quietly wrong.

The version used to be written out three times — pyproject.toml,
cagentic/__init__.py, and mcp_client's CLIENT_INFO — which is two more than can
be kept in step by hand, and the MCP handshake had already drifted into
reporting a number nobody was maintaining.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_pyproject() -> dict:
    """Read pyproject without needing setuptools, the way coding_tools does."""
    try:
        import tomllib
    except ImportError:  # pragma: no cover - only on <3.11
        tomllib = pytest.importorskip("tomli", reason="no TOML parser available")
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_the_version_has_one_home():
    import cagentic

    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'dynamic = ["version"]' in text, "pyproject should read the package's version"
    assert 'version = "' not in text.split("[project]")[1].split("[")[0], (
        "a literal version in [project] would shadow the dynamic one"
    )
    assert re.fullmatch(r"\d+\.\d+\.\d+([abrc.\-+\w]*)?", cagentic.__version__)


def test_the_mcp_handshake_reports_the_real_version():
    import cagentic
    from cagentic.mcp_client import CLIENT_INFO

    assert CLIENT_INFO["version"] == cagentic.__version__


def test_setuptools_resolves_the_dynamic_version():
    """Resolve it the way the build backend will, not by reading the file."""
    ptoml = pytest.importorskip(
        "setuptools.config.pyprojecttoml", reason="setuptools not in this env"
    )
    import cagentic

    resolved = ptoml.read_configuration(str(ROOT / "pyproject.toml"))["project"]
    assert resolved["version"] == cagentic.__version__


def test_the_optional_extras_stay_optional():
    """Core runtime deps are deliberately four. Anything heavier is an extra
    that degrades gracefully when absent."""
    project = load_pyproject()["project"]

    names = {re.split(r"[<>=!\[]", d)[0].strip() for d in project["dependencies"]}
    assert names == {"click", "requests", "prompt_toolkit", "pypdf"}

    extras = project["optional-dependencies"]
    assert "code" in extras and "performance" in extras


def test_requirements_txt_matches_the_real_dependencies():
    project = load_pyproject()["project"]

    listed = sorted(
        line.strip()
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    assert listed == sorted(project["dependencies"])


def test_ci_covers_the_platforms_the_code_branches_on():
    """sandbox.py, shell_session.py, service.py and the Windows shell hunt all
    have per-platform branches. CI used to be Ubuntu and 3.12 alone."""
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for target in ("ubuntu-latest", "macos-latest", "windows-latest"):
        assert target in ci, f"{target} is not in the matrix"
    assert '"3.9"' in ci, "the oldest supported Python should be exercised"


def test_publishing_is_gated_on_a_tag_and_a_green_matrix():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    publish = ci.split("publish:", 1)[1]
    assert "refs/tags/v" in publish, "a push to main must not publish"
    assert "needs: [lint, test]" in publish, "a tag must not ship untested code"
