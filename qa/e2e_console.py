"""Human-level E2E sweep of the osprey-core console (desktop + mobile, light + dark).

Requires Playwright (not a runtime or pytest dependency): `pip install playwright && playwright install chromium`.
Usage: python3 qa/e2e_console.py <base_url> <out_dir> [label]
Writes screenshots and <out_dir>/e2e_receipt_<label>.json with real measured timings. Keep <out_dir>
outside the repository (evidence/ is git-ignored).
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE, OUT = sys.argv[1].rstrip("/"), Path(sys.argv[2])
LABEL = sys.argv[3] if len(sys.argv) > 3 else "local"
OUT.mkdir(parents=True, exist_ok=True)
VARIANTS = [("desktop", 1440, 900), ("mobile", 390, 844)]
THEMES = {"light": "rgb(248, 250, 252)", "dark": "rgb(12, 14, 20)"}
TABS = ["overview", "workflow", "task-backend", "dreams", "receipts"]
BACKENDS = ["farmtable", "github", "jira", "gitlab", "beads", "linear"]
ANOMALIES = re.compile(r"\[object Object\]|\bNaN\b|\bundefined\b|\bnull\b")
EMOJI = re.compile("[\u2190-\u21ff\u2300-\u23ff\u2460-\u27bf\u2900-\u2bff\U0001f000-\U0001faff]")
# Provenance notices that must be visible wherever illustrative or projected data is shown.
SOURCE_LABELS = {
    "overview": ("rollouts", "not live Cloud Run traffic"),
    "workflow": ("workflow", "illustrative stage progress, not live workflow state"),
    "task-backend": ("task-backend", "nothing is created in the selected backend"),
    "dreams": ("dreams", "not recorded from live sessions"),
    "receipts": ("receipts", "not a record of real gate decisions"),
}

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
    const own = Array.from(el.childNodes).some((n) => n.nodeType === 3 && n.textContent.trim());
    if (!own) continue;
    const st = getComputedStyle(el); if (st.visibility === 'hidden' || parseFloat(st.opacity) === 0) continue;
    if (el.closest('dialog') && !el.closest('dialog[open]')) continue;
    const fg = parse(st.color); const bg = bgOf(el); if (!fg) continue;
    const l1 = lum(fg), l2 = lum(bg); const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
    const size = parseFloat(st.fontSize); const bold = parseInt(st.fontWeight, 10) >= 700;
    const need = (size >= 24 || (bold && size >= 18.66)) ? 3 : 4.5; checked += 1; min = Math.min(min, ratio);
    if (ratio < need) failures.push({ text: el.textContent.trim().slice(0, 40), ratio: Math.round(ratio * 100) / 100, need, fg: st.color });
  }
  return { checked, min: Math.round(min * 100) / 100, failures: failures.slice(0, 10), failure_count: failures.length };
}
"""


class Sweep:
    def __init__(self, page, name):
        self.page, self.name, self.steps, self.problems, self.shots = page, name, [], [], []
        self.console, self.page_errors, self.bad_responses = [], [], []
        page.on("console", lambda m: self.console.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: self.page_errors.append(str(e)))
        page.on("response", lambda r: self.bad_responses.append(f"{r.status} {r.url}") if r.status >= 400 else None)

    def step(self, label, func):
        started = time.perf_counter()
        # Each step starts from a clean slate so one failure cannot cascade through an open dialog.
        self.page.evaluate("() => document.querySelectorAll('dialog[open]').forEach((d) => d.close())")
        try:
            detail = func()
            ok = True
        except Exception as exc:  # recorded as a failed step in the receipt
            detail, ok = f"{type(exc).__name__}: {exc}", False
            self.problems.append(f"{label}: {detail}")
        self.steps.append({"step": label, "ok": ok, "ms": round((time.perf_counter() - started) * 1000), "detail": detail})
        return ok

    def check(self, condition, message):
        if not condition:
            raise AssertionError(message)

    def scan(self, where):
        text = self.page.inner_text("body")
        bad = ANOMALIES.findall(text)
        self.check(not bad, f"anomaly strings at {where}: {bad[:5]}")
        self.check(not EMOJI.findall(text), f"emoji/unicode icons at {where}")
        ids = self.page.evaluate("() => Array.from(document.querySelectorAll('[id]')).map((n) => n.id)")
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        self.check(not dupes, f"duplicate ids at {where}: {dupes}")
        overflow = self.page.evaluate("() => [document.documentElement.scrollWidth, window.innerWidth]")
        self.check(overflow[0] <= overflow[1], f"horizontal overflow at {where}: {overflow}")
        captions = self.page.evaluate(
            "() => Array.from(document.querySelectorAll('table caption')).filter((c) => c.getClientRects().length)"
            ".map((c) => c.getBoundingClientRect().width / c.closest('table').getBoundingClientRect().width)")
        self.check(all(ratio >= 0.9 for ratio in captions), f"collapsed table caption at {where}: {captions}")
        self.check(self.page.locator('a[target="_blank"]').count() == 0, "target=_blank link found")
        return {"chars": len(text), "ids": len(ids), "scroll_width": overflow[0]}

    def shot(self, label):
        path = OUT / f"{LABEL}_{self.name}_{label}.png"
        self.page.screenshot(path=str(path), full_page=True)
        self.shots.append(str(path))

    def label_visible(self, kind, phrase):
        notice = self.page.locator(f'[data-source-label="{kind}"]')
        self.check(notice.count() == 1 and notice.first.is_visible(), f"source label {kind} not visible")
        text = notice.first.inner_text()
        self.check(phrase in text, f"source label {kind} lacks '{phrase}': {text}")
        return text


def run_variant(browser, device, width, height, theme):
    context = browser.new_context(viewport={"width": width, "height": height}, color_scheme=theme)
    page = context.new_page()
    sweep = Sweep(page, f"{device}_{theme}")
    page.set_default_timeout(15000)

    def load():
        page.goto(BASE + "/", wait_until="networkidle")
        page.wait_for_selector('body[data-ready="true"]')
        sweep.check(page.get_attribute("html", "data-theme") == theme, "prefers-color-scheme not honored")
        bg = page.evaluate("() => getComputedStyle(document.body).backgroundColor")
        sweep.check(bg == THEMES[theme], f"background {bg} != {THEMES[theme]}")
        sweep.check(page.get_attribute("#theme-toggle", "aria-pressed") == str(theme == "dark").lower(), "aria-pressed mismatch")
        return {"background": bg}

    def tabs():
        result = {}
        for tab in TABS:
            page.click(f"#tab-{tab}")
            panel = page.locator(f"#panel-{tab}")
            sweep.check(page.get_attribute(f"#tab-{tab}", "aria-selected") == "true", f"{tab} not selected")
            sweep.check(panel.is_visible(), f"{tab} panel hidden")
            page.wait_for_function(f"() => !document.querySelector('#panel-{tab} .state-loading')")
            sweep.check(page.locator(f"#panel-{tab} .state-error").count() == 0, f"{tab} rendered an error state")
            sweep.check(page.url.endswith(f"#{tab}"), f"hash not updated for {tab}")
            kind, phrase = SOURCE_LABELS[tab]
            result[tab] = {**sweep.scan(f"tab {tab}"), "source_label": sweep.label_visible(kind, phrase)}
            sweep.shot(f"tab_{tab}")
        return result

    def keyboard():
        page.click("#tab-overview")
        page.keyboard.press("ArrowRight")
        sweep.check(page.get_attribute("#tab-workflow", "aria-selected") == "true", "ArrowRight failed")
        page.keyboard.press("End")
        sweep.check(page.get_attribute("#tab-receipts", "aria-selected") == "true", "End failed")
        page.keyboard.press("Home")
        sweep.check(page.get_attribute("#tab-overview", "aria-selected") == "true", "Home failed")
        page.keyboard.press("ArrowLeft")
        sweep.check(page.get_attribute("#tab-receipts", "aria-selected") == "true", "ArrowLeft wrap failed")
        return "arrow/home/end ok"

    def deep_link():
        page.goto("about:blank")
        page.goto(BASE + "/#dreams", wait_until="networkidle")
        page.wait_for_selector('body[data-ready="true"]')
        sweep.check(page.get_attribute("#tab-dreams", "aria-selected") == "true", "deep link #dreams failed")
        page.wait_for_selector("#summary-score-baseline")
        return page.inner_text("#summary-score-baseline") + " -> " + page.inner_text("#summary-score-candidate")

    def stage_drawers():
        page.click("#tab-workflow")
        page.wait_for_selector(".stage")
        closers = ["escape", "button", "overlay", "escape", "button", "escape"]
        seen = []
        for index, stage in enumerate(page.locator(".stage").all()):
            stage_id = stage.get_attribute("data-stage")
            stage.click()
            page.wait_for_selector("#stage-drawer[open]")
            sweep.check(stage_id in page.inner_text("#stage-drawer-title"), f"drawer title for {stage_id}")
            sweep.check(page.locator("#stage-drawer-body li").count() >= 4, "owners/gates missing")
            sweep.label_visible("workflow-stage", "not live workflow state")
            for _ in range(12):
                page.keyboard.press("Tab")
            inside = page.evaluate("() => document.getElementById('stage-drawer').contains(document.activeElement)")
            sweep.check(inside, "focus escaped the stage drawer")
            sweep.scan(f"stage drawer {stage_id}")
            if index == 3:
                sweep.shot(f"drawer_stage_{stage_id}")
            how = closers[index % len(closers)]
            if how == "escape":
                page.keyboard.press("Escape")
            elif how == "button":
                page.click("#stage-drawer [data-close]")
            else:
                page.mouse.click(5, height // 2)
            page.wait_for_selector("#stage-drawer:not([open])", state="attached")
            back = page.evaluate("() => document.activeElement && document.activeElement.dataset.stage")
            sweep.check(back == stage_id, f"focus not returned to {stage_id} (got {back})")
            seen.append(f"{stage_id}:{how}")
        sweep.check(len(seen) == 6, "expected 6 stages")
        return seen

    def backends():
        page.click("#tab-task-backend")
        page.wait_for_selector(".backend-option")
        keys = {}
        for backend in BACKENDS:
            page.click(f'.backend-option[data-backend="{backend}"]')
            page.wait_for_function(f"() => document.querySelector('#backend-selected')?.textContent === '{backend}'")
            sweep.check(page.get_attribute(f'.backend-option[data-backend="{backend}"]', "aria-checked") == "true", "aria-checked")
            rows = page.locator("#backend-mapping tbody tr")
            sweep.check(rows.count() == 6, f"{backend} mapping rows {rows.count()}")
            keys[backend] = page.inner_text("#backend-mapping tbody tr:first-child td:nth-child(2)").strip()
            server = page.evaluate("async () => (await (await fetch('/api/task-backend')).json()).selected")
            sweep.check(server == backend, f"server selection {server} != {backend}")
        sweep.scan("task backend")
        sweep.shot("task_backend_selected")
        return keys

    def dream_drawer():
        page.click("#tab-dreams")
        page.wait_for_selector("#summary-score-candidate")
        summary = (page.inner_text("#summary-score-baseline"), page.inner_text("#summary-score-candidate"))
        sweep.check(summary == ("60%", "100%"), f"summary scorecard {summary}")
        page.click("#dream-open")
        page.wait_for_selector("#dream-drawer[open]")
        sweep.label_visible("dreams-drawer", "Curated scenario traces")
        rows = page.locator("#dream-drawer tbody tr")
        sweep.check(rows.count() == 10, f"case rows {rows.count()}")
        fails = page.locator('#dream-drawer tbody tr td:nth-child(5) .badge[data-state="FAIL"]').count()
        sweep.check(fails == 4, f"baseline FAIL badges {fails}")
        cand_fails = page.locator('#dream-drawer tbody tr td:nth-child(6) .badge[data-state="FAIL"]').count()
        sweep.check(cand_fails == 0, f"candidate FAIL badges {cand_fails}")
        before = page.inner_text("#replay-meta")
        page.click("#replay-run")
        page.wait_for_function("(b) => { const t = document.getElementById('replay-meta').textContent;"
                               " return t !== b && t.startsWith('Computed on the server'); }", arg=before)
        after = page.inner_text("#replay-meta")
        sweep.check(page.inner_text("#drawer-score-candidate") == "100%", "drawer candidate score")
        sweep.scan("dream drawer")
        sweep.shot("drawer_dp001")
        page.click("#dream-drawer [data-close]")
        page.wait_for_selector("#dream-drawer:not([open])", state="attached")
        sweep.check(page.evaluate("() => document.activeElement.id") == "dream-open", "focus not returned to #dream-open")
        return {"summary": summary, "replay_meta": after}

    def receipts():
        page.click("#tab-receipts")
        page.wait_for_selector("[data-receipt]")
        sweep.check(page.locator("tbody tr[data-receipt]").count() == 7, "receipt rows")
        page.click('[data-verify="RCPT-0003"]')
        page.wait_for_selector('[data-result="RCPT-0003"] .badge[data-state="PASS"]')
        page.click('[data-tamper="RCPT-0004"]')
        page.wait_for_selector('[data-result="RCPT-0004"] .badge[data-state="FAIL"]')
        tamper_text = page.inner_text('[data-result="RCPT-0004"]')
        sweep.check("signature mismatch" in tamper_text, "tamper reason missing")
        page.click("#chain-verify")
        page.wait_for_function("() => document.getElementById('chain-result').textContent.includes('receipts verified')")
        sweep.check(page.locator('#chain-result .badge[data-state="PASS"]').count() == 1, "chain verify not PASS")
        page.click('[data-prev="2"]')
        sweep.check(page.locator('tr.is-highlight[data-seq="2"]').count() == 1, "prev link highlight")
        sweep.scan("receipts")
        sweep.shot("receipts_verified")
        return {"tamper": tamper_text, "chain": page.inner_text("#chain-result")}

    def theme_toggle():
        other = "light" if theme == "dark" else "dark"
        page.click("#theme-toggle")
        sweep.check(page.get_attribute("html", "data-theme") == other, "toggle did not flip theme")
        bg = page.evaluate("() => getComputedStyle(document.body).backgroundColor")
        sweep.check(bg == THEMES[other], f"toggled background {bg}")
        page.reload(wait_until="networkidle")
        page.wait_for_selector('body[data-ready="true"]')
        sweep.check(page.get_attribute("html", "data-theme") == other, "theme not persisted across reload")
        page.click("#theme-toggle")
        sweep.check(page.get_attribute("html", "data-theme") == theme, "toggle back failed")
        return {"toggled_bg": bg}

    def health_refresh():
        page.click("#tab-overview")
        page.click("#overview-refresh")
        page.wait_for_selector('#health-status .badge[data-state]')
        state = page.get_attribute("#health-status .badge", "data-state")
        sweep.check(state == "HEALTHY", f"health state {state}")
        sweep.scan("overview after refresh")
        return state

    def contrast():
        results = {}
        for tab in TABS:
            page.click(f"#tab-{tab}")
            page.wait_for_function(f"() => !document.querySelector('#panel-{tab} .state-loading')")
            report = page.evaluate(CONTRAST_JS)
            results[tab] = report
            sweep.check(report["failure_count"] == 0, f"contrast failures on {tab}: {report['failures']}")
        page.click("#tab-dreams")
        page.click("#dream-open")
        page.wait_for_selector("#dream-drawer[open]")
        results["dream_drawer"] = page.evaluate(CONTRAST_JS)
        sweep.check(results["dream_drawer"]["failure_count"] == 0, f"drawer contrast {results['dream_drawer']['failures']}")
        page.keyboard.press("Escape")
        return {key: {"checked": value["checked"], "min_ratio": value["min"]} for key, value in results.items()}

    for label, func in [("load", load), ("tabs", tabs), ("keyboard", keyboard), ("deep_link", deep_link),
                        ("stage_drawers", stage_drawers), ("task_backends", backends), ("dp001_drawer", dream_drawer),
                        ("receipts", receipts), ("theme_toggle", theme_toggle), ("health_refresh", health_refresh),
                        ("contrast_aa", contrast)]:
        sweep.step(label, func)
    context.close()
    passed = not sweep.problems and not sweep.console and not sweep.page_errors and not sweep.bad_responses
    return {"variant": sweep.name, "viewport": [width, height], "passed": passed, "problems": sweep.problems,
            "console_errors": sweep.console, "page_errors": sweep.page_errors, "http_errors": sweep.bad_responses,
            "steps": sweep.steps, "screenshots": sweep.shots}


def main():
    started = time.perf_counter()
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        results = [run_variant(browser, device, w, h, theme) for device, w, h in VARIANTS for theme in THEMES]
        browser.close()
    receipt = {"base_url": BASE, "label": LABEL, "generated_at": datetime.now(timezone.utc).isoformat(),
               "total_ms": round((time.perf_counter() - started) * 1000),
               "variants_passed": sum(r["passed"] for r in results), "variants_total": len(results), "results": results}
    path = OUT / f"e2e_receipt_{LABEL}.json"
    path.write_text(json.dumps(receipt, indent=2))
    print(json.dumps({"receipt": str(path), "variants_passed": receipt["variants_passed"], "variants_total": len(results),
                      "problems": [p for r in results for p in r["problems"]][:20],
                      "console_errors": sum(len(r["console_errors"]) for r in results),
                      "page_errors": sum(len(r["page_errors"]) for r in results),
                      "http_errors": [e for r in results for e in r["http_errors"]][:10]}, indent=2))
    return 0 if receipt["variants_passed"] == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
