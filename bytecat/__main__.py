import sys

from . import db
from .cli import main

try:
    sys.exit(main())
except db.NoCatalog as e:
    # Last-resort guard only: `status` catches NoCatalog itself, and every
    # other command runs as a child process, so nothing reaches here today.
    print(f"[bytecat] {e}", file=sys.stderr)
    sys.exit(2)
