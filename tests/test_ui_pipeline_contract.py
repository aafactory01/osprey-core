"""UI contract for the live CI/CD panels: containers, accessibility hooks, API-driven stage ids, and safe rendering."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.config_store import REPO_ROOT

STATIC = REPO_ROOT / "app" / "static"
JS_DIR = STATIC / "js"
HTML = (REPO_ROOT / "app" / "templates" / "index.html").read_text(encoding="utf-8")
CSS = (STATIC / "css" / "style.css").read_text(encoding="utf-8")
PIPELINE_JS = (JS_DIR / "pipeline.js").read_text(encoding="utf-8")
WORKFLOW_JS = (JS_DIR / "workflow.js").read_text(encoding="utf-8")
JS_FILES = sorted(JS_DIR.glob("*.js"))
IMPORT = re.compile(r"import\s*\{([^}]*)\}\s*from\s*'\./([\w-]+\.js)'")
EXPORT = re.compile(r"export\s+(?:async\s+)?(?:function|const|let|class)\s+(\w+)")


def _workflow_panel() -> str:
    return HTML.split('id="panel-workflow"', 1)[1].split("</section>", 1)[0]


def test_workflow_panel_has_refresh_control_and_live_meta():
    panel = _workflow_panel()
    assert re.search(r'<button class="btn" type="button" id="pipeline-refresh" aria-controls="workflow-body">', panel)
    assert re.search(r'<output id="pipeline-meta"[^>]*aria-live="polite"', panel)
    assert 'id="workflow-body"' in panel and 'aria-live="polite"' in panel.split('id="workflow-body"', 1)[1].split(">", 1)[0]


def test_sprite_has_monoline_build_and_deploy_icons():
    for name in ("i-build", "i-deploy"):
        symbol = re.search(rf'<symbol id="{name}" viewBox="0 0 24 24">(.*?)</symbol>', HTML)
        assert symbol and "<path" in symbol.group(1) and "fill=" not in symbol.group(1)
    assert "PANEL_ICONS = { ci: 'build', cd: 'deploy' }" in PIPELINE_JS


def test_served_shell_contains_the_pipeline_controls(client):
    html = client.get("/").get_data(as_text=True)
    assert 'id="pipeline-refresh"' in html and 'id="pipeline-meta"' in html


def test_stage_ids_come_from_the_api_never_from_javascript():
    for path in JS_FILES:
        assert not re.search(r"S\d_[A-Z]", path.read_text(encoding="utf-8")), f"{path.name} hardcodes a stage id"
    assert "stage_ids.release_gate" in PIPELINE_JS and "stage_ids.deploy_close" in PIPELINE_JS
    assert "fetch(" not in PIPELINE_JS and "api('/api/pipeline')" in PIPELINE_JS


def test_optional_stage_keys_are_rendered_when_present():
    assert "routeIds(stage.bounce_to)" in WORKFLOW_JS and "stage.pipeline" in WORKFLOW_JS
    assert "pipeline.config_file" in WORKFLOW_JS and "pipeline.command" in WORKFLOW_JS
    assert "export function routeIds" in PIPELINE_JS and "Object.entries(value)" in PIPELINE_JS
    assert "stage.pipeline.config_file" in PIPELINE_JS


def test_panels_are_slots_inside_stage_cards_and_drawers():
    assert "pipelineSlot(stage, 'card')" in WORKFLOW_JS and "pipelineSlot(stage, 'drawer')" in WORKFLOW_JS
    assert "el('li', {}, [stageButton(stage, onOpen), pipelineSlot(stage, 'card')])" in WORKFLOW_JS
    assert "refreshPipeline()" in WORKFLOW_JS and "guardNote()" in WORKFLOW_JS
    assert "pipelineOverview()" in (JS_DIR / "overview.js").read_text(encoding="utf-8")
    main = (JS_DIR / "main.js").read_text(encoding="utf-8")
    assert "initPipelineControls({ button: byId('pipeline-refresh'), output: byId('pipeline-meta') })" in main


def test_status_regions_are_live_and_buttons_are_real_buttons():
    assert "role: 'status', 'aria-live': 'polite'" in PIPELINE_JS
    assert re.search(r"el\('button', \{ class: 'btn btn-small', type: 'button'", PIPELINE_JS)
    assert "'aria-busy'" in PIPELINE_JS and "aria-labelledby" in PIPELINE_JS
    assert "setAttribute('disabled'" not in PIPELINE_JS and ".disabled =" not in PIPELINE_JS


@pytest.mark.parametrize("label", ["Live from", "Not deployed", "Unavailable"])
def test_source_labels_are_rendered_from_the_api_not_hardcoded(label):
    assert label not in PIPELINE_JS
    assert "block.source_label" in PIPELINE_JS and "data.source_label" in PIPELINE_JS


def test_tones_and_icons_come_from_the_api_with_a_safe_fallback():
    assert "data-tone" in PIPELINE_JS and "toneOf(" in PIPELINE_JS
    for status in ("SUCCESS", "FAILURE", "WORKING", "SUCCEEDED", "FAILED"):
        assert status not in PIPELINE_JS, f"{status} tone must come from config via the API"


def test_rendering_is_text_only():
    for path in JS_FILES:
        source = path.read_text(encoding="utf-8")
        assert "innerHTML" not in source and "insertAdjacentHTML" not in source and "outerHTML" not in source
    assert "startsWith('https:')" in PIPELINE_JS and "rel: 'noopener noreferrer'" in PIPELINE_JS


def test_css_has_tone_rules_and_a_single_column_mobile_layout():
    for tone, color in (("ok", "--ok"), ("warn", "--warn"), ("crit", "--crit"), ("neutral", "--text-2")):
        assert re.search(rf'\.badge\[data-tone="{tone}"\] \{{[^}}]*color: var\({color}\)', CSS)
    assert '.timeline.with-pipeline > li[data-live-role="ci"]' in CSS and '.timeline.with-pipeline > li[data-live-role="cd"]' in CSS
    tablet = CSS.split("@media (max-width: 1100px)", 1)[1].split("@media", 1)[0]
    mobile = CSS.split("@media (max-width: 720px)", 1)[1].split("@media", 1)[0]
    assert ".timeline.with-pipeline > li.has-pipeline { grid-column: 1 / -1; }" in tablet
    assert ".timeline.with-pipeline { grid-template-columns: minmax(0, 1fr); }" in mobile


@pytest.mark.parametrize("path", JS_FILES, ids=lambda p: p.name)
def test_every_named_import_is_exported(path: Path):
    for names, target in IMPORT.findall(path.read_text(encoding="utf-8")):
        exported = set(EXPORT.findall((JS_DIR / target).read_text(encoding="utf-8")))
        wanted = {name.strip() for name in names.split(",") if name.strip()}
        assert wanted <= exported, f"{path.name} imports {sorted(wanted - exported)} missing from {target}"


@pytest.mark.parametrize("script", ["qa/e2e_pipeline.py", "qa/e2e_console.py", "qa/smoke_endpoints.py"])
def test_qa_scripts_compile(script):
    source = (REPO_ROOT / script).read_text(encoding="utf-8")
    compile(source, script, "exec")
    if script != "qa/smoke_endpoints.py":
        assert "pipeline" in source and "data-pipeline-role" in source
