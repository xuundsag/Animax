"""Usage: python3 manage.py doctor|service|migrate [arguments]."""

import runpy
import sys

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in {"doctor", "service", "migrate"}:
        raise SystemExit(__doc__)
    command = sys.argv.pop(1)
    runpy.run_module(f"animax.{command}", run_name="__main__")
