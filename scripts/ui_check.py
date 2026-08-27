#!/usr/bin/env python3
"""Local UI regression harness for the ExpenseTracker frontend, driven by Playwright.

Usage:
    scripts/ui_check.py auth
        One-time manual Google sign-in (headed browser). Saves the session for reuse.

    scripts/ui_check.py capture --out scripts/ui_check/baseline [--with-upload path/to/statement.pdf]
        Screenshots the logged-out shell and (if a saved session exists) the authenticated
        shell, at mobile and desktop viewports.

    scripts/ui_check.py diff [--baseline scripts/ui_check/baseline] [--current scripts/ui_check/current]
        Pixel-diffs two capture directories and reports screenshots that changed meaningfully.

The app must already be running locally (./run_api.sh) before calling `auth` or `capture`.
"""
import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = REPO_ROOT / "scripts" / "ui_check"
AUTH_FILE = STATE_DIR / ".auth" / "session.json"

VIEWPORTS = {
    "mobile": {"width": 390, "height": 844},
    "desktop": {"width": 1440, "height": 900},
}

DIFF_PIXEL_THRESHOLD = 200  # number of differing pixels tolerated before flagging a screenshot


def cmd_auth(args):
    base_url = args.base_url
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(viewport=VIEWPORTS["desktop"])
        page = context.new_page()
        page.goto(base_url)
        print("A browser window has opened.")
        print("Complete the Google sign-in flow manually, then return here and press Enter.")
        input()
        try:
            page.wait_for_selector("text=Sign out", timeout=5000)
            print("Detected a signed-in session.")
        except Exception:
            print("Warning: could not confirm 'Sign out' text is visible — saving session anyway.")
        context.storage_state(path=str(AUTH_FILE))
        browser.close()
    print(f"Saved authenticated session to {AUTH_FILE}")


def _log_structural_state(page, label):
    checks = [
        ("upload form", "#upload-form"),
        ("auth button", "#auth-button"),
        ("rules panel", "#rules-panel"),
        ("result panel", "#result-panel"),
    ]
    for name, selector in checks:
        locator = page.locator(selector)
        count = locator.count()
        if count == 0:
            print(f"    [{label}] {name}: NOT FOUND ({selector})")
            continue
        visible = locator.first.is_visible()
        text = ""
        if name == "auth button":
            text = f" text={locator.first.inner_text().strip()!r}"
        print(f"    [{label}] {name}: present, visible={visible}{text}")


def _capture_context(context, page, out_dir, viewport_name, auth_label, base_url, with_upload):
    page.set_viewport_size(VIEWPORTS[viewport_name])
    page.goto(base_url)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1000)  # let client-side auth-restore/htmx settle
    _log_structural_state(page, f"{viewport_name}/{auth_label}")

    shot_path = out_dir / f"{viewport_name}_{auth_label}.png"
    page.screenshot(path=str(shot_path), full_page=True)
    print(f"  saved {shot_path.relative_to(REPO_ROOT)}")

    if with_upload and auth_label == "auth":
        try:
            page.set_input_files("#statement-file", with_upload)
            page.click("#upload-button")
            page.wait_for_selector("#result-panel:not([hidden])", timeout=30000)
            page.wait_for_timeout(500)
            result_path = out_dir / f"{viewport_name}_{auth_label}_result.png"
            page.screenshot(path=str(result_path), full_page=True)
            print(f"  saved {result_path.relative_to(REPO_ROOT)}")
        except Exception as error:
            print(f"  warning: upload flow did not complete cleanly: {error}")


def cmd_capture(args):
    base_url = args.base_url
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    has_session = AUTH_FILE.exists()
    if not has_session:
        print(f"No saved session at {AUTH_FILE} — only the logged-out shell will be captured.")
        print("Run `scripts/ui_check.py auth` first to also capture authenticated panels.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        for viewport_name in VIEWPORTS:
            print(f"[{viewport_name}] logged-out shell")
            context = browser.new_context(viewport=VIEWPORTS[viewport_name])
            page = context.new_page()
            _capture_context(context, page, out_dir, viewport_name, "anon", base_url, None)
            context.close()

            if has_session:
                print(f"[{viewport_name}] authenticated shell")
                context = browser.new_context(
                    viewport=VIEWPORTS[viewport_name], storage_state=str(AUTH_FILE)
                )
                page = context.new_page()
                _capture_context(
                    context, page, out_dir, viewport_name, "auth", base_url, args.with_upload
                )
                context.close()

        browser.close()
    print(f"Capture complete: {out_dir.relative_to(REPO_ROOT)}")


def cmd_diff(args):
    from PIL import Image, ImageChops

    baseline_dir = Path(args.baseline)
    current_dir = Path(args.current)

    if not baseline_dir.exists():
        print(f"No baseline directory at {baseline_dir} — nothing to diff against.")
        return 1

    baseline_files = {f.name: f for f in baseline_dir.glob("*.png")}
    current_files = {f.name: f for f in current_dir.glob("*.png")}

    changed = []
    missing = []
    added = []

    for name, baseline_path in baseline_files.items():
        current_path = current_files.get(name)
        if current_path is None:
            missing.append(name)
            continue
        base_img = Image.open(baseline_path).convert("RGB")
        cur_img = Image.open(current_path).convert("RGB")
        if base_img.size != cur_img.size:
            changed.append((name, f"size changed {base_img.size} -> {cur_img.size}"))
            continue
        diff = ImageChops.difference(base_img, cur_img)
        bbox = diff.getbbox()
        if bbox is None:
            continue
        diff_pixels = sum(1 for pixel in diff.crop(bbox).getdata() if pixel != (0, 0, 0))
        if diff_pixels > DIFF_PIXEL_THRESHOLD:
            changed.append((name, f"{diff_pixels} differing pixels in bbox {bbox}"))

    for name in current_files:
        if name not in baseline_files:
            added.append(name)

    if not changed and not missing:
        print("No meaningful visual differences found.")
    for name in missing:
        print(f"MISSING in current: {name}")
    for name, detail in changed:
        print(f"CHANGED: {name} — {detail}")
    for name in added:
        print(f"NEW (not in baseline): {name}")

    return 1 if (changed or missing) else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Running app URL")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("auth", help="One-time manual login, saves session for reuse")

    capture_parser = sub.add_parser("capture", help="Screenshot the app at mobile/desktop, logged-out/in")
    capture_parser.add_argument("--out", required=True, help="Directory to write screenshots to")
    capture_parser.add_argument(
        "--with-upload", default=None, help="Path to a local PDF statement to drive an upload during capture"
    )

    diff_parser = sub.add_parser("diff", help="Pixel-diff two capture directories")
    diff_parser.add_argument("--baseline", default=str(STATE_DIR / "baseline"))
    diff_parser.add_argument("--current", default=str(STATE_DIR / "current"))

    args = parser.parse_args()

    if args.command == "auth":
        return cmd_auth(args) or 0
    if args.command == "capture":
        return cmd_capture(args) or 0
    if args.command == "diff":
        return cmd_diff(args) or 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
