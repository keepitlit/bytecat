import sys

from . import db
from .cli import main

try:
    sys.exit(main())
except db.NoCatalog as e:
    # Backstop for the commands cli.py runs in-process. The dispatched ones
    # (`ask`, `dupes`, ...) are child processes and handle it themselves.
    print(f"[bytecat] {e}", file=sys.stderr)
    sys.exit(2)
