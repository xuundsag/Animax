from __future__ import annotations

from fractions import Fraction

CAPTION_FONTS = {
    "liberation": "Liberation Sans",
    "dejavu": "DejaVu Sans",
    "dejavu-serif": "DejaVu Serif",
    "dejavu-mono": "DejaVu Sans Mono",
}


FRAME_RATES = {
    "23.976": "24000/1001",
    "24": "24",
    "25": "25",
    "29.97": "30000/1001",
    "30": "30",
    "50": "50",
    "59.94": "60000/1001",
    "60": "60",
}


def video_filter(
    video,
    quality,
    burn=False,
    *,
    language="sub",
    frame_rate="source",
    caption_font="liberation",
):
    width, height = int(video["width"]), int(video["height"])
    try:
        sar = float(
            Fraction(str(video.get("sample_aspect_ratio", "1:1")).replace(":", "/"))
        )
    except (ValueError, ZeroDivisionError):
        sar = 1.0
    if sar <= 0:
        sar = 1.0
    aspect = width * sar / height
    rotation = video.get("tags", {}).get("rotate", 0)
    for side in video.get("side_data_list", []):
        if "rotation" in side:
            rotation = side["rotation"]
    if abs(round(float(rotation))) % 180 == 90:
        aspect = 1 / aspect

    canvas_height = max(18, int(quality) // 18 * 18)
    canvas_width = canvas_height // 9 * 16
    fitted_height = min(canvas_height, canvas_width / aspect)
    fitted_width = fitted_height * aspect
    width = max(2, min(canvas_width, round(fitted_width / 2) * 2))
    height = max(2, min(canvas_height, round(fitted_height / 2) * 2))
    filters = []
    if frame_rate != "source":
        filters.append(f"fps={FRAME_RATES[frame_rate]}")
    filters.extend((f"scale={width}:{height}:flags=bicubic", "setsar=1"))
    if burn:
        try:
            font_name = CAPTION_FONTS[caption_font]
        except KeyError:
            raise ValueError(
                f"CAPTION_FONT must be one of: {', '.join(CAPTION_FONTS)}."
            ) from None
        # libass's WebVTT conversion uses a 288-unit reference height.
        font_size = 14 * canvas_height / height
        margin = round(36 * canvas_height / height)
        border = 3 if language == "dub" else 1
        background = "&H00000000" if language == "dub" else "&HFF000000"
        style = (
            f"FontName={font_name},FontSize={font_size:.2f},"
            "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
            f"BackColour={background},BorderStyle={border},Outline=1,Shadow=0,"
            f"MarginV={margin},Alignment=2"
        )
        filters.append(f"subtitles=subtitles.vtt:fontsdir=fonts:force_style='{style}'")
    filters.append(
        f"pad={canvas_width}:{canvas_height}:(ow-iw)/2:(oh-ih)/2:color=black"
    )
    return ",".join(filters)
