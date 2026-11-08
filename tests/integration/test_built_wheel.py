"""The pack's discovery contract: what the operator approves is what loads.

These run against the installed (editable) distribution, so they catch the two
mistakes that matter here — a manifest that disagrees with the code, and code
that reaches into core — without needing a real wheel install. The full
build-install-enable smoke is the release gate in the README.
"""

from __future__ import annotations

import importlib
import importlib.metadata as metadata
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from research_engine_sdk import parse_manifest

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = PACKAGE_ROOT / "acsm" / "plugin.yaml"

EXPECTED_TOOLS = {
    "acsm.fulfill_document",
    "acsm.scrape_document",
    "acsm.ingest_document",
    "acsm.check_document",
    "acsm.list_documents",
}


@pytest.fixture(scope="module")
def manifest():
    return parse_manifest(MANIFEST_PATH)


def test_entry_point_exposes_the_package():
    entry_points = metadata.entry_points(group="research_engine.plugins")
    matching = [entry for entry in entry_points if entry.name == "acsm"]
    assert matching, "the distribution must advertise one research_engine.plugins entry point"
    assert matching[0].value == "acsm"


def test_manifest_declares_the_document_type_and_tools(manifest):
    assert manifest.plugin_id == "acsm"
    assert [tool.id for tool in manifest.provides.mcp_tools] and {
        tool.id for tool in manifest.provides.mcp_tools
    } == EXPECTED_TOOLS
    types = {entry.id: entry.default_chunker for entry in manifest.provides.document_types}
    assert types == {"drm_pdf": "prose_window"}


def test_manifest_requests_exactly_the_permissions_the_code_uses(manifest):
    permissions = manifest.permissions
    assert permissions.subprocess is True, "the pack spawns Wine, Xvfb, xdotool and import"
    assert permissions.ingest is True, "acsm.ingest_document writes to the corpus"
    assert permissions.filesystem.value == "read_write_plugin_data"
    assert permissions.network.value == "egress"
    assert permissions.network_allowlist == [], "no fixed OCR host may ship in the artefact"
    assert permissions.llm is False and permissions.write is False


def test_handlers_exist_declare_the_same_schema_and_are_served_from_the_manifest(manifest):
    raw = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    raw_tools = {entry["id"]: entry for entry in raw["provides"]["mcp_tools"]}

    for tool in manifest.provides.mcp_tools:
        module_name, attribute = tool.entry.split(":", 1)
        module = importlib.import_module(module_name)
        handler = getattr(module, attribute)

        assert handler._tool_id == tool.id
        assert handler._tool_input_schema == raw_tools[tool.id]["input_schema"], (
            f"{tool.id}: the decorator schema and the approved manifest schema must be identical"
        )
        assert handler._tool_description.strip() == tool.description.strip()


def test_handlers_are_importable_without_core():
    # The handlers must depend on the SDK alone (FR-020).
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import importlib\n"
            "for entry in ('acsm.tools.fulfill_document', 'acsm.tools.scrape_document',\n"
            "              'acsm.tools.ingest_document', 'acsm.tools.check_document',\n"
            "              'acsm.tools.list_documents'):\n"
            "    importlib.import_module(entry)\n"
            "import sys\n"
            "assert not any(m.startswith('research_engine.') for m in sys.modules), \\\n"
            "    'plugin runtime imported core internals'\n",
        ],
        capture_output=True,
        text=True,
        cwd=PACKAGE_ROOT,
    )
    assert result.returncode == 0, result.stderr


def test_package_init_has_no_import_side_effects():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, importlib; importlib.import_module('acsm'); "
            "extra = [m for m in sys.modules if m.startswith(('research_engine_sdk', 'acsm.'))]; "
            "assert not extra, f'importing acsm loaded {extra}'",
        ],
        capture_output=True,
        text=True,
        cwd=PACKAGE_ROOT,
    )
    assert result.returncode == 0, result.stderr


def test_no_runtime_module_imports_core():
    offenders = []
    for path in (PACKAGE_ROOT / "acsm").rglob("*.py"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            imports_core = "research_engine" in stripped and "research_engine_sdk" not in stripped
            if stripped.startswith(("import ", "from ")) and imports_core:
                offenders.append(f"{path.name}:{number}: {stripped}")
    assert not offenders, f"runtime code must import only the SDK: {offenders}"


def test_built_wheel_ships_the_manifest_and_package():
    wheels = sorted((PACKAGE_ROOT / "dist").glob("marginalia_ai_plugin_acsm-*.whl"))
    if not wheels:
        pytest.skip("no built wheel in dist/ — run `uv build --wheel` to check the artefact")
    import zipfile

    with zipfile.ZipFile(wheels[-1]) as archive:
        names = set(archive.namelist())
    assert "acsm/plugin.yaml" in names
    assert "acsm/tools/ingest_document.py" in names
    assert "acsm/__init__.py" in names
