"""Generate the short 1080p transition card inserted before each episode."""

from __future__ import annotations

from pathlib import Path

from .media import run_process


def _filter_path(path: str | Path) -> str:
    value = Path(path).as_posix()
    return (
        value.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace(",", "\\,")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


async def create_title_card(
    directory: Path,
    title: str,
    episode: int,
    *,
    font: str,
    timeout: int = 45,
    max_bytes: int = 8 * 1024**3,
    disk_root: Path | None = None,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / f"transition-{episode:04d}.mp4"
    if output.exists() and output.stat().st_size > 0:
        return output
    title_file = directory / f"title-{episode:04d}.txt"
    subtitle_file = directory / f"episode-{episode:04d}.txt"
    title_file.write_text(title[:160], encoding="utf-8")
    subtitle_file.write_text(f"Starting Episode {episode}", encoding="utf-8")
    font_expr = _filter_path(font)
    title_expr = _filter_path(title_file)
    subtitle_expr = _filter_path(subtitle_file)
    filters = (
        "drawbox=x=0:y=0:w=iw:h=8:color=0x75a7ff@0.95:t=fill,"
        f"drawtext=fontfile={font_expr}:textfile={title_expr}:reload=0:"
        "fontcolor=white:fontsize=68:x=(w-text_w)/2:y=h*0.43:"
        "borderw=2:bordercolor=0x080d16:shadowcolor=black@0.65:shadowx=2:shadowy=3,"
        f"drawtext=fontfile={font_expr}:textfile={subtitle_expr}:reload=0:"
        "fontcolor=0x9dbbff:fontsize=36:x=(w-text_w)/2:y=h*0.56:"
        "borderw=1:bordercolor=0x080d16"
    )
    command = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=0x080d16:s=1920x1080:r=30:d=2.8",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-vf",
        filters,
        "-shortest",
        "-t",
        "2.8",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(output),
    ]
    try:
        await run_process(
            command,
            directory,
            timeout=timeout,
            max_bytes=max_bytes,
            disk_root=disk_root or directory,
        )
    finally:
        title_file.unlink(missing_ok=True)
        subtitle_file.unlink(missing_ok=True)
    return output
