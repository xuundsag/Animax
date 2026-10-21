"""Verified attachment delivery with persisted intent and bounded transport fallback."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import discord

from .diagnostics import redact
from .upload_verify import channel_anchor, verify_upload

log = logging.getLogger("animax.upload")


class UploadFailure(RuntimeError):
    def __init__(
        self,
        message,
        *,
        stage,
        outcome_unknown=False,
        status=None,
        code=None,
        details=None,
    ):
        self.stage, self.outcome_unknown = stage, outcome_unknown
        self.status, self.code = status, code
        self.details = details
        super().__init__(message)


class JournalCommitFailure(UploadFailure):
    pass


def write_report(path: Path, report: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        temporary.chmod(0o600)
        json.dump(report, file, indent=2, ensure_ascii=False)
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(path)
    if os.name == "posix":
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _read_intent(path):
    if path.is_symlink() or path.stat().st_size > 512 * 1024:
        raise ValueError("Unsafe or oversized upload state.")
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("Invalid upload state.")
    for name in ("attempts", "events"):
        if name in data and not isinstance(data[name], list):
            raise ValueError("Invalid upload-state history.")
    if "caption" in data and not isinstance(data["caption"], str):
        raise ValueError("Invalid upload caption.")
    return data


def _transient(exc):
    status = getattr(exc, "status", None)
    if status is not None:
        return 500 <= status < 600
    return isinstance(
        exc, (asyncio.TimeoutError, ConnectionError, aiohttp.ClientError)
    ) or (isinstance(exc, OSError) and exc.errno in {32, 54, 104, 110, 10054})


async def upload_episode(
    target,
    source: Path,
    caption: str,
    *,
    filename: str,
    report_path: Path,
    timeout=1800,
    mode="auto",
    on_phase=None,
    reference=None,
    author_id=None,
    state_path: Path | None = None,
    attempts=2,
    fallback=True,
    verify_timeout=60,
    on_event=None,
    recover_existing=False,
    new_delivery=False,
):
    has_cloud = callable(getattr(target, "upload_files", None))
    chosen = (
        "cloud"
        if mode == "auto" and has_cloud
        else "multipart"
        if mode == "auto"
        else mode
    )
    if chosen not in {"cloud", "multipart"} or not 1 <= attempts <= 3:
        raise ValueError("Invalid upload mode or attempt limit.")
    if chosen == "cloud" and not has_cloud:
        raise UploadFailure(
            "Cloud attachments are unavailable for this channel.", stage="preflight"
        )
    if author_id is None:
        author_id = getattr(getattr(target, "_state", None), "self_id", None)
    if author_id is None:
        raise UploadFailure(
            "The sending account is required for upload verification.",
            stage="preflight",
        )
    state_path = state_path or report_path.with_name(report_path.stem + ".state.json")
    source_stat = source.stat()
    source_identity = (
        source_stat.st_dev,
        source_stat.st_ino,
        source_stat.st_size,
        source_stat.st_mtime_ns,
    )
    identity = {
        "account_id": str(author_id),
        "channel_id": str(target.id),
        "filename": filename,
        "file_size_bytes": source.stat().st_size,
    }
    started = time.monotonic()
    deadline = started + timeout
    report = {
        "version": 1,
        "started_utc": datetime.now(timezone.utc).isoformat(),
        **identity,
        "caption": caption,
        "nonce": str(secrets.randbits(63)),
        "stage": "preflight",
        "method": chosen,
        "timeout_seconds": timeout,
        "message_confirmed": False,
        "outcome_unknown": False,
        "attempts": [],
        "events": [],
    }
    if reference is not None:
        report["reply_to_message_id"] = str(reference.message_id)

    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise asyncio.TimeoutError()
        return value

    def persist(required=False):
        report["elapsed_seconds"] = round(time.monotonic() - started, 2)
        try:
            write_report(state_path, report)
            write_report(report_path, report)
        except OSError:
            if required:
                raise
            log.exception("Could not update upload diagnostic state")

    def notify(text):
        log.info("UPLOAD channel=%s file=%s %s", target.id, filename, text)
        report.setdefault("events", []).append(
            {"elapsed_seconds": round(time.monotonic() - started, 2), "event": text}
        )
        report["events"] = report["events"][-60:]
        if on_event:
            try:
                on_event(text)
            except Exception:
                log.exception("Terminal upload event failed")

    def fail(message, *, unknown=False, exc=None, stage=None):
        report.update(
            stage=stage or report["stage"],
            outcome_unknown=unknown,
            message_confirmed=False,
            error=redact(message)[:1800],
        )
        if exc is not None:
            report.update(
                error_type=type(exc).__name__,
                http_status=getattr(exc, "status", None),
                discord_error_code=getattr(exc, "code", None),
                error=redact(str(getattr(exc, "text", None) or exc))[:1800],
            )
        persist()
        return UploadFailure(
            message,
            stage=report["stage"],
            outcome_unknown=unknown,
            status=report.get("http_status"),
            code=report.get("discord_error_code"),
            details=report.get("error"),
        )

    async def check():
        notify("Checking uploaded message and attachment")
        report["stage"] = "verifying"
        persist()
        result = await verify_upload(
            target, report, timeout=min(verify_timeout, remaining()), notify=notify
        )
        report["verification"] = {"state": result.state, "reason": result.reason}
        notify(result.reason)
        persist()
        return result

    def confirmed(message):
        report.update(
            stage="message_confirmed",
            message_confirmed=True,
            outcome_unknown=False,
            message_id=str(message.id),
            jump_url=getattr(message, "jump_url", None),
        )
        notify(f"Verified message {message.id}; no further send needed")
        persist()
        return message

    resuming_intent = False
    try:
        saved = (
            state_path
            if state_path.exists()
            else report_path
            if recover_existing and report_path.exists()
            else None
        )
        if recover_existing and saved is None:
            raise UploadFailure(
                "Previous send needs review but its upload state is missing; refusing a blind resend.",
                stage="review",
                outcome_unknown=True,
            )
        if saved is not None:
            try:
                previous = _read_intent(saved)
                pending = (
                    recover_existing
                    or previous.get("attempts")
                    or previous.get("outcome_unknown")
                    or previous.get("message_confirmed")
                    or previous.get("message_id")
                    or previous.get("stage") == "Posting message"
                )
                if pending and not (new_delivery and previous.get("message_confirmed")):
                    for key, value in identity.items():
                        # Reports before v2.3.1 did not include the account ID.
                        if key == "account_id" and key not in previous:
                            continue
                        if previous.get(key) != value:
                            raise ValueError(
                                "Previous upload state does not match this account/channel/file."
                            )
                    stamp = datetime.fromisoformat(previous["started_utc"])
                    if stamp.tzinfo is None:
                        raise ValueError(
                            "Upload start timestamp is missing its timezone."
                        )
                    resuming_intent = True
                    report.update(previous)
                    report.update(identity)
                    report.setdefault("caption", caption)
                    report.setdefault("nonce", str(secrets.randbits(63)))
                    report.setdefault("attempts", [])
                    report["attempts"] = report["attempts"][-30:]
                    report.setdefault("events", [])
                    result = await check()
                    if result.state == "found":
                        return confirmed(result.message)
                    if result.state != "absent":
                        raise fail(
                            "Could not safely resolve the previous send; cached video retained.",
                            unknown=True,
                            stage="review",
                        )
                    notify(
                        "Previous send not found after delayed readable checks; retry is allowed"
                    )
            except UploadFailure:
                raise
            except (ValueError, KeyError, TypeError, OSError) as exc:
                # Do not overwrite evidence of a prior send with a fresh intent.
                raise UploadFailure(
                    "Upload state is invalid or mismatched; inspect it before retrying.",
                    stage="review",
                    outcome_unknown=True,
                ) from exc
        if "anchor_id" not in report and not resuming_intent:
            notify("Checking destination access before sending")
            try:
                report["anchor_id"] = await channel_anchor(
                    target, remaining(), author_id
                )
            except Exception as exc:
                raise fail(
                    "Cannot read destination history; no file was sent.",
                    exc=exc,
                    stage="preflight",
                ) from exc
        persist(required=True)
        if on_phase:
            on_phase("Uploading")
        send_options = {"nonce": report["nonce"]}
        if reference is not None:
            send_options.update(reference=reference, mention_author=False)
        method = chosen
        for number in range(attempts):
            current_stat = source.stat()
            if (
                current_stat.st_dev,
                current_stat.st_ino,
                current_stat.st_size,
                current_stat.st_mtime_ns,
            ) != source_identity:
                raise fail(
                    "Prepared video changed during recovery; no further send was attempted.",
                    unknown=bool(report.get("outcome_unknown")),
                    stage="source_changed",
                )
            earlier_uncertain = bool(report.get("outcome_unknown"))
            report.update(stage="Uploading", method=method)
            entry = {"method": method, "number": number + 1, "stage": "uploading"}
            report["attempts"].append(entry)
            persist(required=True)
            notify(
                f"Sending attempt {number + 1}/{attempts} using {method}; {identity['file_size_bytes'] / 1_000_000:.1f} MB"
            )
            attachment = discord.File(str(source), filename=filename)
            posting = False
            error = None
            try:
                outgoing = attachment
                if method == "cloud":
                    cloud_files = await asyncio.wait_for(
                        target.upload_files(attachment), timeout=remaining()
                    )
                    if len(cloud_files) != 1:
                        raise RuntimeError(
                            "Discord did not return exactly one cloud attachment."
                        )
                    outgoing = cloud_files[0]
                report.update(stage="Posting message", outcome_unknown=True)
                entry["stage"] = "posting"
                persist(required=True)
                posting = True
                sent = await asyncio.wait_for(
                    target.send(report["caption"], file=outgoing, **send_options),
                    timeout=remaining(),
                )
                report["message_id"] = str(sent.id)
                entry.update(stage="acknowledged", message_id=str(sent.id))
                persist()
            except asyncio.CancelledError:
                report.update(
                    stage="cancelled",
                    outcome_unknown=posting or report.get("outcome_unknown", False),
                )
                persist()
                raise
            except Exception as exc:
                error = exc
                ambiguous = posting and (
                    not isinstance(exc, discord.HTTPException)
                    or (getattr(exc, "status", 0) or 0) >= 500
                )
                report.update(
                    outcome_unknown=earlier_uncertain or ambiguous,
                    error_type=type(exc).__name__,
                    http_status=getattr(exc, "status", None),
                    discord_error_code=getattr(exc, "code", None),
                    error=redact(str(getattr(exc, "text", None) or exc))[:1800],
                )
                entry.update(
                    stage="error",
                    http_status=report["http_status"],
                    error_type=report["error_type"],
                )
                notify(
                    f"Attempt failed: {type(exc).__name__}, HTTP {report['http_status']}; cached video retained"
                )
                persist()
                if not _transient(exc):
                    if ambiguous:
                        result = await check()
                        if result.state == "found":
                            return confirmed(result.message)
                    raise fail(
                        "Upload stopped; this error is not eligible for automatic retry.",
                        unknown=bool(report.get("outcome_unknown")),
                        exc=exc,
                    ) from exc
            finally:
                attachment.close()
            result = await check()
            if result.state == "found":
                return confirmed(result.message)
            if result.state != "absent":
                raise fail(
                    "Destination verification is inconclusive; no resend was attempted.",
                    unknown=True,
                    exc=error,
                    stage="review",
                )
            if number + 1 >= attempts:
                raise fail(
                    "No delivered message found after the bounded attempts; cached video retained.",
                    unknown=bool(report.get("outcome_unknown")),
                    exc=error,
                    stage="retry_exhausted",
                )
            if fallback and has_cloud:
                method = "multipart" if method == "cloud" else "cloud"
            notify(
                f"No matching message after delayed checks; retrying cached file via {method}"
            )
            persist(required=True)
        raise AssertionError("Upload attempt loop exhausted unexpectedly")
    except asyncio.CancelledError:
        report["stage"] = "cancelled"
        persist()
        raise
    except UploadFailure:
        raise
    except Exception as exc:
        raise fail(
            "Upload recovery stopped; inspect the terminal and upload report.",
            unknown=bool(report.get("outcome_unknown")),
            exc=exc,
        ) from exc
