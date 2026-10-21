"""Atomic, readable anime/episode/message journal. No account tokens or source-stream URLs. Discord attachment URLs may expire."""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


class Library:
    def __init__(self, path: Path, *, data: dict | None = None):
        self.path = path
        supplied = data is not None
        if not supplied:
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            data = json.loads(text) if text.strip() else None
        self.data = (
            {"version": 1, "anime": {}, "jobs": {}}
            if data is None or data == {}
            else data
        )
        if not isinstance(self.data, dict) or self.data.get("version") != 1:
            raise ValueError(
                "Invalid anime.json. Restore anime.json.bak before continuing; history will not be silently erased."
            )
        self.data.setdefault("anime", {})
        self.data.setdefault("jobs", {})
        self.data.setdefault("routes", {})
        if not isinstance(self.data["anime"], dict) or not isinstance(
            self.data["jobs"], dict
        ):
            raise ValueError("Invalid anime.json structure. Restore a backup.")
        if not isinstance(self.data.get("routes", {}), dict):
            raise ValueError("Invalid anime.json routing structure. Restore a backup.")
        if not supplied:
            self.migrate_legacy()
            if not self.path.exists() or not text.strip() or data == {}:
                self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            backup = self.path.with_suffix(".json.bak")
            shutil.copyfile(self.path, backup)
            backup.chmod(0o600)
        temporary = self.path.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as file:
            json.dump(self.data, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        temporary.chmod(0o600)
        temporary.replace(self.path)

    def migrate_legacy(self):
        legacy = self.path.parent / "state.json"
        if not legacy.exists() or self.data.get("legacy_imported"):
            return
        records = json.loads(legacy.read_text())
        for key, entry in records.items():
            parts = key.split(":")
            if len(parts) != 5 or not isinstance(entry, dict):
                continue
            account, channel, mal, lang, episode = parts
            ids = entry.get("message_ids", [])
            if lang not in {"sub", "dub"} or not all(
                p.isdigit() for p in (account, channel, mal, episode)
            ):
                continue
            if len(ids) == 1:
                self.record(
                    int(mal),
                    f"MAL {mal}",
                    lang,
                    int(episode),
                    int(account),
                    int(channel),
                    "uploaded",
                    save=False,
                    message_id=ids[0],
                    message_ids=ids,
                    legacy=True,
                )
        self.data["legacy_imported"] = now()
        self.save()

    def record(
        self,
        mal,
        title,
        language,
        episode,
        account,
        channel,
        status,
        *,
        save=True,
        **details,
    ):
        anime = self.data["anime"].setdefault(
            str(mal), {"title": title, "episodes": {}}
        )
        if title and not title.startswith("MAL "):
            anime["title"] = title
        episode_data = (
            anime["episodes"]
            .setdefault(language, {})
            .setdefault(str(episode), {"deliveries": {}})
        )
        key = f"{account}:{channel}"
        row = episode_data["deliveries"].setdefault(
            key,
            {
                "account_id": str(account),
                "channel_id": str(channel),
                "attempts": 0,
                "messages": [],
            },
        )
        if status == "downloading":
            row["attempts"] += 1
        # A failed forced resend must not erase knowledge of a previously delivered message.
        row.update(status=status, updated_at=now(), **details)
        if status == "uploaded" and details.get("message_id"):
            row["error"] = None
            message = {
                "message_id": str(details["message_id"]),
                "uploaded_at": now(),
                "jump_url": details.get("jump_url"),
                "size_bytes": details.get("size_bytes"),
                "download_url": details.get("download_url"),
                "actual_language": details.get("actual_language", language),
                "guild_id": details.get("guild_id"),
                "reply_to_message_id": details.get("reply_to_message_id"),
            }
            existing = next(
                (
                    m
                    for m in row["messages"]
                    if m["message_id"] == message["message_id"]
                ),
                None,
            )
            if existing is None:
                row["messages"].append(message)
            else:
                for key, value in message.items():
                    if value is not None and key != "uploaded_at":
                        existing[key] = value
        if save:
            self.save()

    def completed(self, mal, language, episode, account, channel):
        try:
            row = self.data["anime"][str(mal)]["episodes"][language][str(episode)][
                "deliveries"
            ][f"{account}:{channel}"]
            return (
                bool(row.get("messages"))
                and not row.get("needs_review")
                and row.get("status") != "uploading"
            )
        except KeyError:
            return False

    def job(self, job_id, **data):
        previous = self.data["jobs"].get(job_id)
        record = dict(previous or {"created_at": now()})
        record.update(data, updated_at=now())
        self.data["jobs"][job_id] = record
        try:
            self.save()
        except Exception:
            if previous is None:
                self.data["jobs"].pop(job_id, None)
            else:
                self.data["jobs"][job_id] = previous
            raise

    def latest_job(self, account, *, active_only=False):
        records = [
            dict(record, id=job_id)
            for job_id, record in self.data["jobs"].items()
            if str(record.get("account_id")) == str(account)
            and (
                not active_only
                or record.get("status") in {"queued", "running", "interrupted"}
            )
        ]
        return max(records, key=lambda r: r.get("updated_at", ""), default=None)

    def summary(self, mal):
        anime = self.data["anime"].get(str(mal))
        if not anime:
            return f"No tracked episodes for MAL {mal} yet."
        lines = [f"{anime['title']} · MAL {mal}"]
        for lang, episodes in anime["episodes"].items():
            delivered = sum(
                any(r.get("messages") for r in e["deliveries"].values())
                for e in episodes.values()
            )
            lines.append(
                f"{lang.upper()}: {delivered}/{len(episodes)} tracked episodes delivered"
            )
        return "\n".join(lines)

    def previous_message(self, mal, episode, account, channel, preferred_language=None):
        candidates = []
        anime = self.data["anime"].get(str(mal), {})
        for language, episodes in anime.get("episodes", {}).items():
            for number, entry in episodes.items():
                if not str(number).isdigit() or int(number) >= episode:
                    continue
                row = entry.get("deliveries", {}).get(f"{account}:{channel}", {})
                messages = row.get("messages") or []
                if messages:
                    last = messages[-1]
                    candidates.append(
                        (
                            int(number),
                            language == preferred_language,
                            last.get("uploaded_at", ""),
                            last,
                        )
                    )
        return max(candidates, key=lambda row: row[:3])[3] if candidates else None
