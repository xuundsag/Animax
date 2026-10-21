"""ANIMAX entry point; bootstrap errors are also timestamped for terminal logs."""

from __future__ import annotations

import sys
import time


def _startup_error(label: str, exc: Exception):
    print(
        f"[{time.strftime('%H:%M:%S')}] {label}: {type(exc).__name__}: {exc}",
        file=sys.stderr,
        flush=True,
    )
    raise SystemExit(1) from None


try:
    from animax.bootstrap import ensure_runtime
except Exception as exc:
    _startup_error("ANX-BOOTSTRAP", exc)


if __name__ == "__main__":
    try:
        ensure_runtime()
    except SystemExit:
        raise
    except Exception as exc:
        _startup_error("ANX-BOOTSTRAP", exc)

    try:
        from animax.bot import main
    except Exception as exc:
        _startup_error("ANX-STARTUP", exc)

    main()
