import sys

import pytest

from animax.diagnostics import error_hint, error_summary, redact
from animax.media import MediaProcessError, run_process


def test_redaction_and_hints():
    message = "ERROR: HTTP Error 403 at https://example.com/video?token=secret signature=secret2"
    safe = redact(message)
    assert "secret" not in safe and "403" in safe
    assert "provider rejected" in error_hint(message).lower()
    assert "yt-dlp is missing" in error_hint("No module named yt_dlp")
    assert "Chromium OS dependencies" in error_hint(
        "libnspr4.so: error while loading shared libraries"
    )


def test_error_summary_keeps_actual_cause():
    text = "Progress\nERROR: Requested format is not available\nMore chatter"
    assert error_summary(text) == "ERROR: Requested format is not available"


@pytest.mark.asyncio
async def test_subprocess_error_includes_real_stderr_and_exit_code(tmp_path):
    with pytest.raises(MediaProcessError) as error:
        await run_process(
            [
                sys.executable,
                "-c",
                "import sys;print('ERROR: HTTP Error 403: https://example.com/stream?token=secret',file=sys.stderr);sys.exit(7)",
            ],
            tmp_path,
        )
    assert error.value.exit_code == 7
    assert "403" in str(error.value)
    assert "secret" not in str(error.value)
    assert "exited with code 7" in str(error.value)
    # Full local log remains available; only the display is redacted.
    assert "secret" in (tmp_path / "process.log").read_text()


@pytest.mark.asyncio
async def test_process_error_does_not_include_previous_operation(tmp_path):
    (tmp_path / "process.log").write_text("ERROR: stale failure\n")
    with pytest.raises(MediaProcessError) as error:
        await run_process(
            [
                sys.executable,
                "-c",
                "import sys;print('ERROR: new failure');sys.exit(1)",
            ],
            tmp_path,
        )
    assert "new failure" in str(error.value)
    assert "stale failure" not in str(error.value)
