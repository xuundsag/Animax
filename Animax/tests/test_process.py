import asyncio
from pathlib import Path

import pytest

from animax.media import MediaError, run_process


@pytest.mark.asyncio
async def test_subprocess_cancel_reaps(tmp_path):
    import sys

    pidfile = tmp_path / "pid.txt"
    task = asyncio.create_task(
        run_process(
            [
                sys.executable,
                "-c",
                "import os,time,pathlib; pathlib.Path('pid.txt').write_text(str(os.getpid())); time.sleep(60)",
            ],
            tmp_path,
        )
    )
    for _ in range(100):
        if pidfile.exists():
            break
        await asyncio.sleep(0.02)
    assert pidfile.exists()
    pid = int(pidfile.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    if Path("/proc").exists():
        assert not Path(f"/proc/{pid}").exists()


@pytest.mark.asyncio
async def test_timeout(tmp_path):
    import sys

    with pytest.raises(MediaError, match="timed out"):
        await run_process(
            [sys.executable, "-c", "import time;time.sleep(20)"], tmp_path, timeout=0.1
        )
