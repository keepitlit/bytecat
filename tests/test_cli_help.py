#!/usr/bin/env python3
"""Every flag and argument of every command has --help text.

Records each add_argument call while a command builds its parser for --help,
so a flag added later without help= fails here rather than shipping blank.
"""
import argparse
import contextlib
import importlib
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))


COMMANDS = ["cli", "scan", "merge", "hash", "extract", "embed", "enrich", "ask",
            "report_dupes", "report_coverage", "audit", "eval", "forget"]

seen: list = []
real = argparse._ActionsContainer.add_argument


def recording(self, *args, **kwargs):
    action = real(self, *args, **kwargs)
    prog = getattr(self, "prog", "") or getattr(getattr(self, "_container", None), "prog", "")
    seen.append((prog, action))
    return action


argparse._ActionsContainer.add_argument = recording
try:
    for name in COMMANDS:
        mod = importlib.import_module(f"bytecat.{name}")
        seen.clear()
        argv = sys.argv
        sys.argv = [name, "--help"]
        try:
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                (mod.main(["--help"]) if name in ("audit", "forget") else mod.main())
        except SystemExit:
            pass
        finally:
            sys.argv = argv
        blank = sorted({f"{prog} {'/'.join(a.option_strings) or a.dest}"
                        for prog, a in seen
                        if a.help is None and not isinstance(a, argparse._HelpAction)})
        check(f"{name}: every flag has help", not blank, ", ".join(blank))
finally:
    argparse._ActionsContainer.add_argument = real

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
