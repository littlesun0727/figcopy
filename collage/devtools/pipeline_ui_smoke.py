"""Exercise review and independent-layer editing in a real local Chromium using fixtures."""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw

from ..core.io import atomic_write_json, read_json, sha256_file
from ..projects import DataPaths
from ..providers import ProviderAudit
from ..studio.workbench.application import WorkbenchApplication
from ..studio.workbench.server import create_workbench_server


class ReviewFixture:
    """Answer only synthetic smoke-test questions; never call a network provider."""

    name = "ui-smoke-fixture"
    requested_model = "fixture"
    fixture = True

    def analyze(self, reference_bytes, **kwargs):
        assert "两件" in kwargs["prompt"]
        result = _draft()
        result["questions"] = []
        return result, ProviderAudit(
            self.name, "fixture", "fixture", "ui-fixture", True, 0
        )


def _draft():
    overlays = []
    for identifier, label, rect, kind, color in [
        ("circle", "独立圆形", [60, 120, 150, 150], "ellipse", "#EE8F61"),
        ("frame", "独立边框", [280, 300, 200, 180], "dashed_rectangle", "#6CC9C4"),
    ]:
        overlays.append(
            {
                "id": identifier,
                "label": label,
                "source_rect": rect,
                "target_rect": rect,
                "attachment": None,
                "action": "basic_shape",
                "generation_brief": "",
                "requires_exact_content": False,
                "review_notes": "synthetic fixture",
                "shape": {
                    "kind": kind,
                    "fill": color if kind == "ellipse" else None,
                    "outline": color,
                    "width": 5,
                    "radius": 0,
                    "dash": 14,
                    "gap": 8,
                },
            }
        )
    return {
        "slots": [],
        "overlays": overlays,
        "questions": ["这两件装饰需要分开移动吗？"],
        "background": {"background_brief": "plain", "review_notes": "fixture"},
        "layer_order": [
            {"type": "background"},
            *({"type": "overlay", "id": item["id"]} for item in overlays),
        ],
    }


class Browser:
    def __init__(self, executable: Path, profile: Path):
        import websocket

        self.process = subprocess.Popen(
            [
                str(executable),
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                "--remote-debugging-port=0",
                "--user-data-dir=" + str(profile),
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        active = profile / "DevToolsActivePort"
        deadline = time.monotonic() + 20
        port = None
        while time.monotonic() < deadline:
            try:
                # Chromium creates this file before releasing its Windows write
                # handle; existence alone does not mean it is readable yet.
                port = int(active.read_text(encoding="utf-8").splitlines()[0])
                break
            except (OSError, ValueError, IndexError):
                if self.process.poll() is not None:
                    break
                time.sleep(0.1)
        if port is None:
            self.process.terminate()
            self.process.wait(timeout=5)
            raise RuntimeError("Browser debug endpoint did not become ready")
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json", timeout=5
        ) as response:
            target = next(
                item for item in json.load(response) if item["type"] == "page"
            )
        self.socket = websocket.create_connection(
            target["webSocketDebuggerUrl"], timeout=20, suppress_origin=True
        )
        self.counter = 0
        self.errors = []
        self.call("Runtime.enable")
        self.call("Page.enable")
        self.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": 1380, "height": 960, "deviceScaleFactor": 1, "mobile": False},
        )

    def call(self, method, params=None):
        self.counter += 1
        self.socket.send(
            json.dumps({"id": self.counter, "method": method, "params": params or {}})
        )
        while True:
            message = json.loads(self.socket.recv())
            if message.get("method") == "Runtime.exceptionThrown":
                self.errors.append(message["params"]["exceptionDetails"].get("text"))
            if message.get("id") == self.counter:
                if "error" in message:
                    raise RuntimeError(message["error"])
                return message.get("result", {})

    def js(self, expression):
        result = self.call(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
        )
        if "exceptionDetails" in result:
            raise RuntimeError(result["exceptionDetails"])
        return result.get("result", {}).get("value")

    def until(self, expression):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.js(expression):
                return
            time.sleep(0.1)
        raise RuntimeError(
            "Browser condition timed out: "
            + expression
            + " state="
            + str(self.js("document.body.innerText.slice(-800)"))
        )

    def screenshot(self, path):
        result = self.call(
            "Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False}
        )
        path.write_bytes(base64.b64decode(result["data"]))

    def close(self):
        try:
            self.call("Browser.close")
        except Exception:
            pass
        self.socket.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    root = args.out.resolve()
    root.mkdir(parents=True, exist_ok=True)
    if (root / "validation.json").exists():
        raise RuntimeError("Use a new output directory for each UI run")
    image = Image.new("RGB", (600, 760), "#F5F0E8")
    draw = ImageDraw.Draw(image)
    draw.ellipse((60, 120, 210, 270), fill="#EE8F61")
    draw.rectangle((280, 300, 480, 480), outline="#6CC9C4", width=5)
    image.save(root / "reference.png")
    Image.new("RGB", image.size, "#F5F0E8").save(root / "background.png")
    atomic_write_json(root / "draft.json", _draft())
    app = WorkbenchApplication(DataPaths.resolve(root / "data"))
    app.workflow.start(
        "ui-smoke",
        root / "reference.png",
        reviewer="fixture-tester",
        manual_draft_path=root / "draft.json",
        background_candidate_path=root / "background.png",
        vision_provider_spec="collage.devtools.pipeline_ui_smoke:ReviewFixture",
        open_review=False,
    )
    server = create_workbench_server(port=0, application=app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    browser = None
    try:
        with tempfile.TemporaryDirectory(
            prefix="figcopy-browser-", ignore_cleanup_errors=True
        ) as profile:
            browser = Browser(args.browser, Path(profile))
            base = f"http://127.0.0.1:{server.server_address[1]}"
            browser.call("Page.navigate", {"url": base + "/projects/ui-smoke/review"})
            browser.until("document.querySelector('#questions textarea') !== null")
            assert browser.js("document.querySelector('#otherFeedback').value") == ""
            assert (
                browser.js("document.querySelector('#finalConfirmed').checked") is False
            )
            browser.screenshot(root / "01_questions.png")
            browser.js("""document.querySelector('#questions textarea').value='两件需要分开';
                document.querySelector('#otherFeedback').value='保留两件独立装饰';
                document.querySelector('#revise').click();""")
            browser.until(
                "document.querySelector('#status').textContent.includes('已生成纠正后的结果')"
            )
            assert (
                browser.js("document.querySelector('#finalConfirmed').checked") is False
            )
            browser.screenshot(root / "02_corrected.png")
            browser.js(
                "document.querySelector('#finalConfirmed').click(); document.querySelector('#save').click();"
            )
            browser.until("location.pathname === '/projects/ui-smoke'")
            browser.until("document.body.innerText.includes('检查最终合成效果')")
            original = app.store.open("ui-smoke")
            original_hash = sha256_file(original.template / "template.json")
            assets = {
                item["id"]: item["sha256"]
                for item in read_json(original.template / "template.json")["assets"]
            }
            browser.call("Page.navigate", {"url": base + "/projects/ui-smoke/layers"})
            browser.until(
                "document.querySelector('#status')?.textContent.includes('每件装饰都可单独调整')"
            )
            browser.screenshot(root / "03_layers.png")
            before = browser.js("payload()")
            # Dispatch actual pointer events, then use ordinary numeric controls.
            point = browser.js(
                "(() => {const b=document.querySelector('#view').getBoundingClientRect(); return {x:b.left+130*b.width/600,y:b.top+190*b.height/760};})()"
            )
            browser.call(
                "Input.dispatchMouseEvent",
                {"type": "mousePressed", **point, "button": "left", "clickCount": 1},
            )
            browser.call(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseMoved",
                    "x": point["x"] + 65,
                    "y": point["y"] + 30,
                    "button": "left",
                    "buttons": 1,
                },
            )
            browser.call(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseReleased",
                    "x": point["x"] + 65,
                    "y": point["y"] + 30,
                    "button": "left",
                    "clickCount": 1,
                },
            )
            browser.js("""const width=document.querySelector('#width'); width.value='190'; width.dispatchEvent(new Event('change'));
                const rotation=document.querySelector('#rotation'); rotation.value='25'; rotation.dispatchEvent(new Event('change'));
                document.querySelector('#up').click();""")
            after = browser.js("payload()")
            assert before != after
            browser.screenshot(root / "04_moved.png")
            browser.js("document.querySelector('#preview').click()")
            browser.until(
                "document.querySelector('#status').textContent.includes('已用正式本地 renderer 合成')"
            )
            browser.screenshot(root / "05_preview.png")
            browser.js("document.querySelector('#save').click()")
            browser.until("location.pathname.startsWith('/projects/ui-smoke-layout-')")
            browser.until("document.body.innerText.includes('检查最终合成效果')")
            project_id = browser.js("location.pathname.split('/')[2]")
            target = app.store.open(project_id)
            assert sha256_file(original.template / "template.json") == original_hash
            assert {
                item["id"]: item["sha256"]
                for item in read_json(target.template / "template.json")["assets"]
            } == assets
            assert not browser.errors, browser.errors
            browser.screenshot(root / "06_new_version.png")
            atomic_write_json(
                root / "validation.json",
                {
                    "status": "passed",
                    "fixture": True,
                    "real_model_calls": 0,
                    "questions_shown": True,
                    "other_default_empty": True,
                    "correction_and_manual_confirmation": True,
                    "pointer_drag": True,
                    "scale_rotate_reorder": True,
                    "local_exact_preview": True,
                    "new_version_saved": True,
                    "original_manifest_unchanged": True,
                    "asset_hashes_unchanged": True,
                    "browser_exceptions": browser.errors,
                },
            )
            browser.close()
            browser = None
    finally:
        if browser:
            browser.close()
        server.shutdown()
        server.server_close()
    print(
        json.dumps(
            {
                "status": "passed",
                "fixture": True,
                "checks": "review + independent layers",
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
