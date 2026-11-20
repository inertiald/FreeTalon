"""`python -m freetalon.pairing` runs the pairing server.

Configuration is environment-only; see CONTRACT.md (Configuration).
"""

from .server import main

if __name__ == "__main__":
    raise SystemExit(main())
