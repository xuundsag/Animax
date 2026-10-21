"""Internal paced yt-dlp entry point. No immediate network retries after HTTP 429."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import yt_dlp
from yt_dlp.networking.exceptions import HTTPError

if __package__:
    from .rate_limits import parse_retry_after
else:
    from rate_limits import parse_retry_after

RATE_LIMIT_EXIT = 86


class StopForRateLimit(Exception):
    pass


class PacedDownloader(yt_dlp.YoutubeDL):
    def __init__(
        self,
        params,
        *,
        request_interval: float,
        rate_limit_report: Path,
        progress_file: Path | None = None,
    ):
        self.progress_file = progress_file
        self.last_progress_write = 0.0
        self.request_interval = max(0, request_interval)
        self.rate_limit_report = rate_limit_report
        self._lock = threading.Lock()
        self._next_request = 0.0
        self._rate_limited = False
        params.setdefault("progress_hooks", []).append(self.progress_hook)
        super().__init__(params)

    def progress_hook(self, data):
        if not self.progress_file:
            return
        finished = data.get("status") == "finished"
        if not finished and time.monotonic() - self.last_progress_write < 0.4:
            return
        self.last_progress_write = time.monotonic()
        total = data.get("total_bytes") or data.get("total_bytes_estimate")
        downloaded = data.get("downloaded_bytes") or 0
        index, count = data.get("fragment_index"), data.get("fragment_count")
        fraction = (
            downloaded / total
            if total
            else (index / count if index is not None and count else None)
        )
        payload = {
            "fraction": 1
            if finished
            else min(1, max(0, fraction))
            if fraction is not None
            else None,
            "estimated": not bool(data.get("total_bytes")),
            "downloaded": downloaded,
            "total": total,
            "speed": data.get("speed"),
            "eta": data.get("eta"),
            "fragment": index,
            "fragments": count,
            "status": data.get("status"),
        }
        temporary = self.progress_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        temporary.replace(self.progress_file)

    def process_info(self, info):
        # Resume native fragments only when the selected source/format is unchanged.
        work = self.rate_limit_report.parent
        identity = {
            key: info.get(key)
            for key in ("format_id", "width", "height", "vcodec", "acodec")
        }
        identity["source_path"] = urlsplit(info.get("url", "")).path
        identity["fragment_paths"] = [
            urlsplit(f.get("url", "")).path for f in (info.get("fragments") or [])
        ]
        fingerprint = hashlib.sha256(
            json.dumps(identity, sort_keys=True).encode()
        ).hexdigest()
        signature = work / "source-fingerprint.txt"
        if signature.exists() and signature.read_text() != fingerprint:
            for file in work.glob("source.*"):
                if file.is_file():
                    file.unlink()
        signature.write_text(fingerprint)
        return super().process_info(info)

    def urlopen(self, request):
        # All native manifest, key and fragment requests go through this method.
        # A single lock enforces spacing even if a downloader starts helper threads.
        with self._lock:
            if self._rate_limited:
                raise StopForRateLimit("HTTP 429: further CDN requests are paused.")
            delay = self._next_request - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._next_request = time.monotonic() + self.request_interval
            try:
                return super().urlopen(request)
            except HTTPError as exc:
                if exc.status != 429:
                    raise
                self._rate_limited = True
                retry_after = parse_retry_after(exc.response.headers.get("Retry-After"))
                self.rate_limit_report.write_text(
                    json.dumps(
                        {
                            "status": 429,
                            "retry_after": retry_after,
                        }
                    ),
                    encoding="utf-8",
                )
                exc.close()
                raise StopForRateLimit(
                    "HTTP 429: stopped immediately; the job must wait before retrying."
                ) from None


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--mp-request-interval", type=float, default=0.25)
    parser.add_argument("--mp-rate-limit-report", type=Path, required=True)
    parser.add_argument("--mp-progress-file", type=Path)
    args, downloader_args = parser.parse_known_args()
    args.mp_rate_limit_report.unlink(missing_ok=True)
    options = yt_dlp.parse_options(downloader_args)
    params = options.ydl_opts
    # Native HLS only, so request spacing and Retry-After capture cover fragments.
    params.update(
        {
            "concurrent_fragment_downloads": 1,
            "retries": 0,
            "fragment_retries": 0,
            "extractor_retries": 0,
            "skip_unavailable_fragments": False,
            "external_downloader": {"m3u8": "native"},
        }
    )
    try:
        with PacedDownloader(
            params,
            request_interval=args.mp_request_interval,
            rate_limit_report=args.mp_rate_limit_report,
            progress_file=args.mp_progress_file,
        ) as downloader:
            result = downloader.download(options.urls)
        if args.mp_rate_limit_report.exists():
            raise SystemExit(RATE_LIMIT_EXIT)
        raise SystemExit(result or 0)
    except StopForRateLimit as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(RATE_LIMIT_EXIT)
    except Exception:
        if args.mp_rate_limit_report.exists():
            print(
                "ERROR: HTTP 429; all further fragment requests were stopped.",
                file=sys.stderr,
            )
            raise SystemExit(RATE_LIMIT_EXIT)
        raise


if __name__ == "__main__":
    main()
