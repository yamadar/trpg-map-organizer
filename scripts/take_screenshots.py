"""README 用のスクリーンショットを撮影する (開発用、playwright が必要).

事前準備:
    pip install playwright
    playwright install chromium
    python -m http.server -d docs 8081 &

実行:
    python -m scripts.take_screenshots --port 8081
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "screenshots"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--out", type=Path, default=OUTPUT_DIR)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    base = f"http://localhost:{args.port}/"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        # ----- 1. メイン画面 (デスクトップ JA) -----
        ctx = browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="ja-JP",
            device_scale_factor=2,
        )
        page = ctx.new_page()
        page.goto(base, wait_until="networkidle")
        page.wait_for_function("document.getElementById('count').textContent !== '—'")
        time.sleep(0.5)
        out = args.out / "01-main-ja.png"
        page.screenshot(path=str(out), full_page=False)
        print(f"saved: {out}")

        # ----- 2. テーマでフィルタリング (クトゥルフ + 海) -----
        page.goto(base + "#g=" + "クトゥルフ" + "&t=" + "海", wait_until="networkidle")
        page.wait_for_function("document.querySelectorAll('.card').length > 0")
        time.sleep(0.5)
        out = args.out / "02-filtered-cthulhu.png"
        page.screenshot(path=str(out), full_page=False)
        print(f"saved: {out}")

        # ----- 3. プレビューモーダル (最初のカードをクリック) -----
        page.goto(base + "#g=" + "中世", wait_until="networkidle")
        page.wait_for_function("document.querySelectorAll('.card').length > 0")
        time.sleep(0.5)
        page.locator(".card").first.click()
        page.wait_for_selector("#preview[open]", timeout=5000)
        page.wait_for_function(
            "document.getElementById('preview-img').complete && "
            "document.getElementById('preview-img').naturalWidth > 0"
        )
        time.sleep(0.8)
        out = args.out / "03-preview-modal.png"
        page.screenshot(path=str(out), full_page=False)
        print(f"saved: {out}")
        ctx.close()

        # ----- 4. 英語 UI -----
        ctx = browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            device_scale_factor=2,
        )
        page = ctx.new_page()
        page.goto(base, wait_until="networkidle")
        page.wait_for_function("document.getElementById('count').textContent !== '—'")
        time.sleep(0.5)
        out = args.out / "04-main-en.png"
        page.screenshot(path=str(out), full_page=False)
        print(f"saved: {out}")
        ctx.close()

        # ----- 5. モバイル (375x812 = iPhone X 相当) -----
        ctx = browser.new_context(
            viewport={"width": 390, "height": 844},
            locale="ja-JP",
            device_scale_factor=3,
            user_agent=(
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148"
            ),
        )
        page = ctx.new_page()
        page.goto(base, wait_until="networkidle")
        page.wait_for_function("document.getElementById('count').textContent !== '—'")
        time.sleep(0.5)
        out = args.out / "05-mobile-ja.png"
        page.screenshot(path=str(out), full_page=False)
        print(f"saved: {out}")

        # ----- 6. モバイル プレビューモーダル -----
        page.goto(base + "#g=" + "メルヘン", wait_until="networkidle")
        page.wait_for_function("document.querySelectorAll('.card').length > 0")
        time.sleep(0.5)
        page.locator(".card").first.click()
        page.wait_for_selector("#preview[open]", timeout=5000)
        page.wait_for_function(
            "document.getElementById('preview-img').complete && "
            "document.getElementById('preview-img').naturalWidth > 0"
        )
        time.sleep(0.8)
        out = args.out / "06-mobile-preview.png"
        page.screenshot(path=str(out), full_page=False)
        print(f"saved: {out}")

        browser.close()

    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
