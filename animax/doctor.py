"""Check dependencies/browser and optionally the downloader's real CDN access.
No Discord token/login or full episode download is used by this diagnostic.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from .diagnostics import dependency_report, error_hint, error_summary, redact
from .rate_limits import Cooldown, RateLimitError

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / ".browsers"))


async def check_source(arguments):
    # Import after the basic checks so missing modules produce a useful report.
    from playwright.async_api import async_playwright

    from .media import run_process, write_cookie_file
    from .resolver import Resolver

    if arguments is None:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            await browser.close()
        return {"browser": "launch passed", "source": "not requested"}

    cooldown = Cooldown(ROOT / "cdn_cooldown.json")
    if cooldown.remaining() > 0:
        raise RuntimeError(
            f"CDN cooldown is active ({cooldown.remaining():.0f}s remaining). Source check deferred; do not keep probing."
        )
    mal, language, episode = arguments
    if language not in {"dub", "sub"} or int(mal) < 1 or int(episode) < 1:
        raise ValueError("Use --source MAL_ID dub|sub EPISODE with positive numbers.")
    with tempfile.TemporaryDirectory(prefix="megaplay-doctor-") as folder:
        work = Path(folder)
        async with Resolver(90, True) as resolver:
            async with resolver.resolve(int(mal), language, int(episode)) as stream:
                cookies = work / "cookies.txt"
                await write_cookie_file(stream, cookies)
                rate_report = work / "rate-limit.json"
                args = [
                    sys.executable,
                    str(ROOT / "animax" / "downloader.py"),
                    "--mp-request-interval",
                    "2",
                    "--mp-rate-limit-report",
                    str(rate_report),
                    "--ignore-config",
                    "--no-playlist",
                    "--no-progress",
                    "--socket-timeout",
                    "20",
                    "--retries",
                    "1",
                    "--cookies",
                    str(cookies),
                    "--list-formats",
                ]
                for key, value in stream.headers.items():
                    args += ["--add-header", f"{key}:{value}"]
                args += ["--", stream.url]
                try:
                    output = await run_process(args, work, timeout=120)
                except Exception:
                    if rate_report.exists():
                        data = json.loads(rate_report.read_text())
                        if data.get("status") == 429:
                            cooldown.defer(max(300, data.get("retry_after") or 0))
                    raise
                finally:
                    cookies.unlink(missing_ok=True)
                    if (work / "process.log").exists():
                        logs = ROOT / "logs"
                        logs.mkdir(exist_ok=True)
                        destination = logs / "doctor-process.log"
                        shutil.copyfile(work / "process.log", destination)
                        destination.chmod(0o600)
    return {
        "browser": "launch passed",
        "mal_id": int(mal),
        "language": language,
        "episode": int(episode),
        "downloader_cdn_manifest": "passed",
        "formats_output": redact(output)[-3500:],
        "notice": "Metadata only. No full episode or Discord upload tested.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", nargs=3, metavar=("MAL_ID", "LANGUAGE", "EPISODE"))
    args = parser.parse_args()
    report = {"result": "failed", "discord_login": False}
    try:
        report["dependencies"] = dependency_report()
        report["checks"] = asyncio.run(check_source(args.source))
        report["result"] = "passed"
    except Exception as exc:
        if isinstance(exc, RateLimitError):
            Cooldown(ROOT / "cdn_cooldown.json").defer(max(300, exc.retry_after or 0))
        report["error"] = error_summary(f"{type(exc).__name__}: {exc}", 2500)
        report["hint"] = error_hint(str(exc))
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    destination = logs / "doctor-report.json"
    destination.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    destination.chmod(0o600)
    print(json.dumps(report, indent=2))
    print(f"Diagnostic report saved: {destination}")
    if report["result"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
