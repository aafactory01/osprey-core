"""Playwright checks for the live CI/CD panels (release gate: Cloud Build, deploy close: Cloud Deploy).

Requires Playwright (not a runtime or pytest dependency). Usage:
  python3 qa/e2e_pipeline.py <base_url> <out_dir> <label> --expect not_configured|unavailable|fixture
Variants: desktop 1440x900 light and mobile 390x844 dark. Stage ids come from GET /api/pipeline `stage_ids`.
`--expect fixture` replaces the /api/pipeline response inside the browser with a rendering fixture built from
the real document (desktop: SUCCESS build + SUCCEEDED rollout, mobile: FAILURE + FAILED) to exercise the live
rendering path; fixture labels say so, and the receipt marks the run as a fixture, never as live data.
Writes screenshots and <out_dir>/e2e_pipeline_<label>.json with measured timings; exits 0 only if all pass.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parent.parent
TONES = json.loads((REPO / "config" / "pipeline_status.json").read_text(encoding="utf-8"))
VARIANTS = [("desktop", 1440, 900, "light"), ("mobile", 390, 844, "dark")]
BACKGROUND = {"light": "rgb(248, 250, 252)", "dark": "rgb(12, 14, 20)"}
ANOMALIES = re.compile(r"\[object Object\]|\bNaN\b|\bundefined\b|\bnull\b")
EMOJI = re.compile("[\u2190-\u21ff\u2300-\u23ff\u2460-\u27bf\u2900-\u2bff\U0001f000-\U0001faff]")
FIXTURE_NOTE = " [QA rendering fixture, not live data]"
STATE = {"ci": "status", "cd": "rollout_state"}
# WCAG AA contrast over every visible text node (same method as qa/e2e_console.py; closed dialogs are skipped).
CONTRAST_JS = """
() => {
  const parse = (c) => { const m = c.match(/rgba?\\(([^)]+)\\)/); if (!m) return null;
    const p = m[1].split(',').map((x) => parseFloat(x)); return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 }; };
  const lum = ({ r, g, b }) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
  const bgOf = (el) => { const layers = []; for (let n = el; n; n = n.parentElement) { const c = parse(getComputedStyle(n).backgroundColor);
      if (c && c.a > 0) { layers.push(c); if (c.a >= 1) break; } }
    let out = parse(getComputedStyle(document.documentElement).backgroundColor) || { r: 255, g: 255, b: 255, a: 1 };
    for (const l of layers.reverse()) { out = { r: l.r * l.a + out.r * (1 - l.a), g: l.g * l.a + out.g * (1 - l.a), b: l.b * l.a + out.b * (1 - l.a), a: 1 }; }
    return out; };
  const failures = []; let min = 99; let checked = 0;
  for (const el of document.querySelectorAll('body *')) {
    if (!el.getClientRects().length) continue;
    if (!Array.from(el.childNodes).some((n) => n.nodeType === 3 && n.textContent.trim())) continue;
    const st = getComputedStyle(el); if (st.visibility === 'hidden' || parseFloat(st.opacity) === 0) continue;
    if (el.closest('dialog') && !el.closest('dialog[open]')) continue;
    const fg = parse(st.color); if (!fg) continue; const bg = bgOf(el);
    const l1 = lum(fg), l2 = lum(bg); const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
    const size = parseFloat(st.fontSize); const bold = parseInt(st.fontWeight, 10) >= 700;
    const need = (size >= 24 || (bold && size >= 18.66)) ? 3 : 4.5; checked += 1; min = Math.min(min, ratio);
    if (ratio < need) failures.push({ text: el.textContent.trim().slice(0, 40), ratio: Math.round(ratio * 100) / 100, need });
  }
  return { checked, min: Math.round(min * 100) / 100, failures: failures.slice(0, 10), failure_count: failures.length };
}
"""


def fixture_doc(real: dict, failing: bool) -> dict:
    """Live-shaped document for rendering checks; tones come from config/pipeline_status.json like the server's."""
    doc = copy.deepcopy(real)
    ci_status, cd_state = ("FAILURE", "FAILED") if failing else ("SUCCESS", "SUCCEEDED")
    ci_tones, cd_tones = TONES["ci"]["status_tones"], TONES["cd"]["status_tones"]
    digest = "sha256:" + "0123456789abcdef" * 4
    steps = [("pytest", "SUCCESS", 61000), ("docker-build", "FAILURE" if failing else "SUCCESS", 95000),
             ("push", "QUEUED" if failing else "SUCCESS", None if failing else 12000)]
    doc["source_label"] = real["source_label"] + FIXTURE_NOTE
    doc["provenance"].update(commit_sha="f" * 40, short_sha="fffffff", build_id="fixture-build-0001", image_digest=digest,
                             release="rel-fixture-0001", pipeline=real["cd"]["pipeline"], target=real["cd"]["target"],
                             revision="osprey-core-fixture-00001-abc", service="osprey-core", deployed_by_cloud_deploy=True)
    doc["ci"].update(status=ci_status, tone=ci_tones.get(ci_status), build_id="fixture-build-0001", image_tag="fffffff",
                     image_digest=digest, create_time="2026-11-14T20:00:00.000Z", finish_time="2026-11-14T20:03:00.000Z",
                     duration_ms=175000, status_detail="step docker-build failed" if failing else None,
                     steps=[{"id": i, "status": s, "tone": ci_tones.get(s), "duration_ms": d} for i, s, d in steps],
                     digest_matches_provenance=True, source="live", error=None, latency_ms=140,
                     source_label=TONES["ci"]["live_label"] + FIXTURE_NOTE)
    doc["cd"].update(release="rel-fixture-0001", rollout="rel-fixture-0001-to-prod-0001", rollout_state=cd_state,
                     tone=cd_tones.get(cd_state), deploy_start_time="2026-11-14T20:04:00.000Z",
                     deploy_end_time="2026-11-14T20:05:30.000Z", failure_reason="CLOUD_RUN_REVISION_FAILED" if failing else None,
                     source="live", error=None, latency_ms=150, source_label=TONES["cd"]["live_label"] + FIXTURE_NOTE)
    return doc


class Sweep:
    def __init__(self, page, name, out, label):
        self.page, self.name, self.out, self.label = page, name, out, label
        self.steps, self.problems, self.shots, self.console, self.page_errors, self.http_errors = [], [], [], [], [], []
        self.pipeline_requests = 0
        page.on("console", lambda m: self.console.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: self.page_errors.append(str(e)))
        page.on("response", lambda r: self.http_errors.append(f"{r.status} {r.url}") if r.status >= 400 else None)
        page.on("request", lambda r: self._count(r.url))

    def _count(self, url):
        if url.endswith("/api/pipeline"):
            self.pipeline_requests += 1

    def step(self, label, func):
        started = time.perf_counter()
        try:
            detail, ok = func(), True
        except Exception as exc:  # a failed step is part of the receipt
            detail, ok = f"{type(exc).__name__}: {exc}", False
            self.problems.append(f"{label}: {detail}")
        self.steps.append({"step": label, "ok": ok, "ms": round((time.perf_counter() - started) * 1000), "detail": detail})

    def check(self, condition, message):
        if not condition:
            raise AssertionError(message)

    def scan(self, where):
        text = self.page.inner_text("body")
        self.check(not ANOMALIES.findall(text), f"anomaly strings at {where}: {ANOMALIES.findall(text)[:5]}")
        self.check(not EMOJI.findall(text), f"emoji/unicode icons at {where}")
        ids = self.page.evaluate("() => Array.from(document.querySelectorAll('[id]')).map((n) => n.id)")
        self.check(len(ids) == len(set(ids)), f"duplicate ids at {where}")
        width = self.page.evaluate("() => [document.documentElement.scrollWidth, window.innerWidth]")
        self.check(width[0] <= width[1], f"horizontal overflow at {where}: {width}")
        clipped = self.page.evaluate("() => Array.from(document.querySelectorAll('.pipeline-panel *')).filter((n) => "
                                     "n.getClientRects().length && n.getBoundingClientRect().right > window.innerWidth + 1).length")
        self.check(clipped == 0, f"{clipped} panel elements overflow the viewport at {where}")
        self.check(self.page.locator('a[target="_blank"]').count() == 0, "target=_blank link found")
        return {"chars": len(text), "scroll_width": width[0]}

    def shot(self, label, selector=None):
        path = self.out / f"{self.label}_{self.name}_{label}.png"
        (self.page.locator(selector).first.screenshot(path=str(path)) if selector
         else self.page.screenshot(path=str(path), full_page=True))
        self.shots.append(str(path))


def run_variant(browser, args, out, device, width, height, theme):
    context = browser.new_context(viewport={"width": width, "height": height}, color_scheme=theme)
    page = context.new_page()
    page.set_default_timeout(15000)
    sweep = Sweep(page, f"{device}_{theme}", out, args.label)
    if args.expect == "fixture":
        def fulfil(route):
            real = route.fetch().json()
            route.fulfill(status=200, content_type="application/json", body=json.dumps(fixture_doc(real, device == "mobile")))
        page.route("**/api/pipeline", fulfil)
    doc = {}

    def load():
        page.goto(args.base + "/#workflow", wait_until="networkidle")
        page.wait_for_selector('body[data-ready="true"]')
        page.wait_for_selector('.timeline [data-pipeline-role="cd"]')
        doc.update(page.evaluate("async () => (await (await fetch('/api/pipeline')).json())"))
        background = page.evaluate("() => getComputedStyle(document.body).backgroundColor")
        sweep.check(background == BACKGROUND[theme], f"background {background}")
        return {"stage_ids": doc["stage_ids"], "ci_source": doc["ci"]["source"], "cd_source": doc["cd"]["source"]}

    def panels():
        sweep.check(page.locator(".timeline [data-pipeline-role]").count() == 2, "expected exactly two live panels")
        result = {}
        for role, key in (("ci", "release_gate"), ("cd", "deploy_close")):
            block, stage_id = doc[role], doc["stage_ids"][key]
            panel = page.locator(f'li:has(> button.stage[data-stage="{stage_id}"]) [data-pipeline-role="{role}"]')
            sweep.check(panel.count() == 1 and panel.is_visible(), f"{role} panel missing under {stage_id}")
            status = panel.locator(f'[data-pipeline-status="{role}"]')
            sweep.check(status.get_attribute("role") == "status" and status.get_attribute("aria-live") == "polite", "status region a11y")
            chip = status.locator(".badge").first
            sweep.check(chip.get_attribute("data-status") == block[STATE[role]], f"{role} chip {chip.get_attribute('data-status')}")
            sweep.check(chip.get_attribute("data-tone") == block["tone"], f"{role} tone {chip.get_attribute('data-tone')}")
            source = panel.locator(f'[data-pipeline-source="{role}"]').inner_text().strip()
            sweep.check(source == block["source_label"], f"{role} source line {source!r}")
            text = panel.inner_text()
            if args.expect == "not_configured":
                sweep.check(block["source"] == "not_configured" and "not configured" in text, f"{role} not honestly not configured")
            if args.expect == "unavailable":
                sweep.check(block["source"] == "unavailable" and block["error"]["taxonomy"] in status.inner_text(), f"{role} taxonomy")
                sweep.check("from revision env" in text or role == "cd", f"{role} provenance fallback not marked")
            if args.expect == "fixture":
                sweep.check(block["source"] == "live" and FIXTURE_NOTE.strip() in text, f"{role} fixture not labelled")
            if role == "ci":
                for target in block["bounce_to"]:
                    sweep.check(target in text, f"bounce route {target} missing")
                sweep.check(panel.locator(".pipeline-steps li").count() == len(block["steps"]), "step rows")
                if block["image_digest"]:
                    sweep.check(block["image_digest"][:19] in text and block["image_digest"][:20] not in text, "digest not shortened to 12 chars")
            else:
                sweep.check(" -> ".join(block["control_loop"]) in text, "control loop text missing")
                sweep.check(f"{block['pipeline']} -> {block['target']}" in text, "pipeline -> target missing")
            sweep.shot(f"panel_{role}", f'li:has(> button.stage[data-stage="{stage_id}"])')
            result[role] = {"status": block[STATE[role]], "tone": block["tone"], "source_label": source}
        sweep.scan("workflow panels")
        report = page.evaluate(CONTRAST_JS)
        sweep.check(report["failure_count"] == 0, f"workflow contrast failures: {report['failures']}")
        result["contrast"] = {"checked": report["checked"], "min_ratio": report["min"]}
        sweep.shot("tab_workflow")
        return result

    def settled(selector):
        page.wait_for_function("(s) => document.querySelector(s).getAttribute('aria-busy') === 'false'", arg=selector)

    def refetch(trigger, selector):
        with page.expect_response(lambda r: r.url.endswith("/api/pipeline")) as response:
            trigger()
        settled(selector)
        return response.value.status

    def refresh():
        before = sweep.pipeline_requests
        statuses = [refetch(lambda: page.click("#pipeline-refresh"), "#pipeline-refresh")]
        page.focus("#pipeline-refresh")
        statuses.append(refetch(lambda: page.keyboard.press("Enter"), "#pipeline-refresh"))
        meta = page.inner_text("#pipeline-meta")
        sweep.check(statuses == [200, 200] and sweep.pipeline_requests >= before + 2, f"refresh: {statuses}, {sweep.pipeline_requests}")
        sweep.check("round trip" in meta and "cache age" in meta, f"meta line {meta!r}")
        sweep.check(page.locator(".timeline [data-pipeline-role]").count() == 2, "panels lost after refresh")
        return {"requests": sweep.pipeline_requests - before, "meta": meta}

    def drawers():
        seen = {}
        for role, key in (("ci", "release_gate"), ("cd", "deploy_close")):
            stage_id = doc["stage_ids"][key]
            page.click(f'button.stage[data-stage="{stage_id}"]')
            page.wait_for_selector("#stage-drawer[open]")
            section = page.locator(f'#stage-drawer [data-pipeline-kind="drawer"] [data-pipeline-role="{role}"]')
            sweep.check(section.count() == 1 and section.is_visible(), f"{role} drawer section missing")
            notice = page.locator(f'[data-source-label="pipeline-{role}-drawer"]')
            sweep.check(notice.count() == 1 and doc["source_label"] in notice.inner_text(), "drawer source notice")
            text = section.inner_text()
            sweep.check(doc[role]["source_label"] in text and "commit sha" in text, f"{role} drawer detail/provenance")
            button = f'#stage-drawer [data-pipeline-refresh="{role}"]'
            page.focus(button)
            sweep.check(refetch(lambda: page.keyboard.press("Enter"), button) == 200, "drawer refresh did not re-fetch")
            inside = page.evaluate("() => document.getElementById('stage-drawer').contains(document.activeElement)")
            sweep.check(inside, "focus left the drawer after refresh")
            for _ in range(15):
                page.keyboard.press("Tab")
            sweep.check(page.evaluate("() => document.getElementById('stage-drawer').contains(document.activeElement)"), "focus trap")
            section.evaluate("(node) => node.scrollIntoView({ block: 'start' })")
            report = page.evaluate(CONTRAST_JS)
            sweep.check(report["failure_count"] == 0, f"{role} drawer contrast failures: {report['failures']}")
            sweep.scan(f"{role} drawer")
            sweep.shot(f"drawer_{role}", "#stage-drawer .drawer-inner")
            page.keyboard.press("Escape")
            page.wait_for_selector("#stage-drawer:not([open])", state="attached")
            back = page.evaluate("() => document.activeElement && document.activeElement.dataset.stage")
            sweep.check(back == stage_id, f"focus not returned to {stage_id}: {back}")
            seen[role] = len(text)
        return seen

    def overview():
        page.click("#tab-overview")
        page.wait_for_selector("#overview-pipeline .badge[data-tone]")
        chips = page.locator("#overview-pipeline .badge[data-tone]").count()
        sweep.check(chips == 2, f"overview chips {chips}")
        sweep.scan("overview")
        sweep.shot("overview_pipeline", "#overview-pipeline")
        page.click('#overview-pipeline a[href="#workflow"]')
        page.wait_for_function("() => document.getElementById('tab-workflow').getAttribute('aria-selected') === 'true'")
        return {"chips": chips}

    for label, func in [("load", load), ("panels", panels), ("refresh", refresh), ("drawers", drawers), ("overview", overview)]:
        sweep.step(label, func)
    context.close()
    passed = not (sweep.problems or sweep.console or sweep.page_errors or sweep.http_errors)
    return {"variant": sweep.name, "viewport": [width, height], "passed": passed, "problems": sweep.problems,
            "console_errors": sweep.console, "page_errors": sweep.page_errors, "http_errors": sweep.http_errors,
            "steps": sweep.steps, "screenshots": sweep.shots}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base")
    parser.add_argument("out")
    parser.add_argument("label")
    parser.add_argument("--expect", choices=["not_configured", "unavailable", "fixture"], required=True)
    args = parser.parse_args()
    args.base = args.base.rstrip("/")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        results = [run_variant(browser, args, out, *variant) for variant in VARIANTS]
        browser.close()
    receipt = {"base_url": args.base, "label": args.label, "expect": args.expect, "fixture": args.expect == "fixture",
               "generated_at": datetime.now(timezone.utc).isoformat(), "total_ms": round((time.perf_counter() - started) * 1000),
               "variants_passed": sum(r["passed"] for r in results), "variants_total": len(results), "results": results}
    path = out / f"e2e_pipeline_{args.label}.json"
    path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(json.dumps({"receipt": str(path), "variants_passed": receipt["variants_passed"], "variants_total": len(results),
                      "problems": [p for r in results for p in r["problems"]][:20],
                      "console_errors": sum(len(r["console_errors"]) for r in results),
                      "http_errors": [e for r in results for e in r["http_errors"]][:10]}, indent=2))
    return 0 if receipt["variants_passed"] == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
