#!/usr/bin/env python3
"""The copy budget: every string a person reads from bytecat is one sentence of
at most 12 words, and a {placeholder} is one word. Explainers grow back as a
second sentence, or a clause explaining the first, when no gate counts words.
This one counts them inside tests/run_all.sh. It needs no model server or
Chroma, so it never skips.

**The bar, as a CLI reads it.** A screen is one command's output for one
outcome: success, empty, refused and failed are four screens. A string stays
only if it names what the person is looking at or choosing, names an action,
tells them something they cannot see and need now to act correctly (an
irreversible consequence, data that will be lost, a privacy fact, why
something they expect is missing), or is required word for word by an
accepted decision. --help is a reference: each line names what a verb or flag
does. Agents read this output too, and pay for every word on every turn.

**House rules for words** (each names the job a word must do, not a list of
banned words):
1. A status line states the one fact the person acts on, and stops. ("Both
   off-diagonal cells are empty: bytecat's verdict is confirmed by a fresh
   read of the disk under SHA-256." → "bytecat and the disk agree.")
2. An error names what failed and the one thing to do about it.
3. A validation message states the limit as the fix: "Must be after {time}".
4. The reason for a rule lives in a source comment. The screen asks for the action.
5. A clause whose only job is to say nothing bad happened has no job.
6. Do not describe what the screen already shows (a count, pill, name or control).
7. Say a fact once, on the thing it is about, not in a line above the list.
8. A line that is only sometimes true is drawn only when it is true.
9. A heading separates two or more groups. One thing is named by itself.
10. Use the person's words, not the system's: "Sent back", not "Needs attention".
    A button is the verb, with no object the screen already names: "Save".

**What it does not catch.** Short narration stays with the rules above: the
budget is a cap, not the bar.

**An exception** is a comment on the line directly above the string, or above
the keyword, assignment or call that holds it:
    # copy-budget: README Guarantees "Local only" - <the job these words do>
    # copy-budget: Alex 2026-10-11: "…" - <the job these words do>
citing a README.md "Guarantees" entry by its bold name, or a dated Alex
quote. The README form comes first, so a marker in a published repo carries
no private names. A marker citing neither fails, so an agent cannot grant
itself one; so does a marker above a string that is within budget. The count
is pinned in EXPECTED_EXCEPTIONS, so a new one shows in two places in the diff.

What is read and skipped, and why, is in copy_budget_scan.py. The scan is
proved on fixtures (part a) before it is trusted on the tree (part b).
"""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import copy_budget_scan as cb  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"\n{detail}" if detail and not cond else ""))


SHORTEN = "Shorten it; if the reason matters, put it in a source comment."
MARKER_FORM = '# copy-budget: README Guarantees "<bold name>" - <the job these words do>'

CONFIG = cb.Config(always_calls=frozenset({
    "print", "eprint",                # stdout, and scan.py's stderr
    "write", "add", "_table",         # report lines: fh.write, report_dupes' add, audit's tables
    "exit", "SystemExit", "error",    # sys.exit("…") and ap.error("…") print their message
    # Domain errors whose message the commands print as "[bytecat] {e}".
    "RuntimeError", "NoCatalog", "LMStudioDown", "LocalOnlyError",
}))

# Lists pinned by count, so adding to one shows in two places in the diff.
# Content the person asked to read: held to truth and plainness, not one line.
NOT_COPY = {}
EXPECTED_NOT_COPY = 0
# Text that steers a model, per file: its constants and their pinned word count.
# The count may fall (lower the pin with it) and may rise only under a marker.
MODEL_TEXT = {
    "bytecat/enrich.py": (("SYSTEM", "VISION_SYSTEM"), 123),
    "bytecat/rerank.py": (("SYSTEM",), 97),
}
EXPECTED_MODEL_TEXT = 3
# Non-prose a first run flags, each with why it is not copy.
SKIP = {
    "bytecat/ask.py": {"STOPWORDS": "words dropped from keyword queries; never shown"},
}
EXPECTED_SKIP = 1
# Over budget with no ruling behind them yet, each waiting on its card. Pinned by
# exact text: a reworded string leaves the list and must pass on its own.
PENDING = {
    ("bytecat/audit.py",
     "[bytecat] {root} is not under any catalogued mount never scanned: bytecat scan "
     "<path> | bytecat merge scanned at another path: mount it there"):
        "keep-it-lit-foss W2 - the refusal guards against a scan that prunes a drive",
    ("bytecat/audit.py",
     "## Proof and limits PROVES: {what} were re-read now and hashed with SHA-256 (the "
     "catalog uses BLAKE2b-128). DOES NOT PROVE: independence of authorship. One author "
     "wrote this check and the code it checks, so a mistake made in both survives. For an "
     "outside check, run hashdeep on a manifest from `--emit-manifest`."):
        "keep-it-lit-foss W2 - the limit of what an audit proves",
}
EXPECTED_PENDING = 2
# Valid markers in the tree. Adding one changes this number too.
EXPECTED_EXCEPTIONS = 0

_README_CITE = re.compile(r'^README Guarantees "([^"]+)"\s+-{1,2}\s+(.+)$')


def guarantees(readme: str) -> set:
    """The bold names of README.md's "## Guarantees" entries."""
    section = re.search(r"^## Guarantees\n(.*?)(?=^## |\Z)", readme, re.M | re.S)
    return set(re.findall(r"^- \*\*(.+?)\.?\*\*", section.group(1), re.M)) if section else set()


def citer(names):
    """Why a marker's ruling does not hold, or None when it does."""
    def cite(body):
        m = _README_CITE.match(body)
        if m is None:
            return cb.quote_citation(body, 'a README "Guarantees" entry (README Guarantees "Local only")')
        if m.group(1) not in names:
            return f'cites README Guarantees "{m.group(1)}", which README.md does not have'
        return None if cb.has_job(m.group(2)) else "names no job for the words"
    return cite


# ── a: the scanner, on fixtures ─────────────────────────────────────────────

CITE = citer({"Local only", "Read-only"})
FIXTURE = "bytecat/fixture.py"


def scan(source, **lists):
    return cb.scan_py(FIXTURE, source, CONFIG, **lists)


def read(source, **lists):
    return [p.text for p in scan(source, **lists).pieces]


def judged(source, pins=None, **lists):
    return cb.judge([scan(source, **lists)], CITE, pins)


def part_a_counting():
    print("a: counting")
    check("a word has a letter or digit; a {placeholder} is one; · › are none",
          cb.words("[bytecat] hashed {n} files: 3 unreadable · {} › {len(rows)}") == 8)
    ones = ["see e.g. the README", "scanned at 7 a.m. today", "started 07:00", "1.5 GB read",
            "wait... then re-run", "pipeline complete."]
    check("e.g., a.m., 07:00, 1.5, an ellipsis and a trailing period end no sentence",
          [cb.sentences(t) for t in ones] == [1] * len(ones), [cb.sentences(t) for t in ones])
    check(". ! or ? before more words ends one, after a closing quote or Markdown ** too",
          [cb.sentences(t) for t in ("Failed. Re-run.", "Gone! Now what?", 'It said "stop." Then left',
                                     "**The disk disagrees.** Do not act.")] == [2, 2, 2, 2])
    twelve = "one two three four five six seven eight nine ten eleven twelve"
    check("over budget past 12 words or past one sentence",
          [cb.over(twelve), cb.over(twelve + " thirteen"), cb.over("Done. Re-run.")] == [False, True, True])
    o = judged('print("Nothing has been deleted or moved. This is a report; you decide.")').offenders[0]
    check("names each offender by file:line, its words and sentences, and its text",
          cb.line(o) == f'{FIXTURE}:1  12 words, 2 sentences  "Nothing has been deleted or moved. '
                        'This is a report; you decide."', cb.line(o))


def part_a_read():
    print("a: what is read")
    check("print, eprint, help=, errors and sys.exit, however short",
          read('print("ok")\neprint("[bytecat] UUID")\nap.add_argument("-x", help="UUID")\n'
               'raise LMStudioDown("no model")\nsys.exit("gone")\nap.error("no roots")')
          == ["ok", "[bytecat] UUID", "UUID", "no model", "gone", "no roots"])
    check("a const or return value only when it is prose",
          read('HINT = "start one or set BYTECAT_LLM_BASE"\nKINDS = "pdf docx xlsx epub"\n'
               'def f():\n    return "the drive is not catalogued yet"')
          == ["start one or set BYTECAT_LLM_BASE", "the drive is not catalogued yet"])
    src = ('print("[bytecat] " + name + " failed; pipeline stopped")\n'
           'print(f"hashed {len(rows)} of {sum(r.size for r in rows)} bytes")\n'
           'print("a copy is on " "the target" if ok else "the copy is missing")\n'
           'print("hashed {} files".format(n))\nprint("hashed %d files" % n)\n'
           'print(f"{when:%A at %H}: scanned")')
    check("a + chain, f-string holes, implicit joins, both arms, .format and %",
          read(src) == ["[bytecat] {name} failed; pipeline stopped", "hashed {len(rows)} of {…} bytes",
                        "a copy is on the target", "the copy is missing", "hashed {} files",
                        "hashed %d files", "{when}: scanned"], read(src))
    doc = '"""bytecat ask: find a file by what it says, on any drive."""\nimport argparse\n'
    check("a module docstring only when the module passes __doc__ on",
          [read(doc + "ap = argparse.ArgumentParser(description=__doc__)"), read(doc)]
          == [["bytecat ask: find a file by what it says, on any drive."], []])


def part_a_skipped():
    print("a: what is skipped")
    src = '''"""bytecat merge: fold every scan file into the one catalog."""
def f(x: "the type of it") -> "a type for the return":
    """Docstrings are for whoever reads the code, not the person."""
    "a bare string is not read by the person"
    row = {"the key is a path": 1, "help": "the drive to check against the target"}
    if mode == "the coverage mode is on":
        pass
    v = row["the file is on the target"]
    assert ok, "the drive should be catalogued by now"
    log.info("the walk finished early on the drive")
    logging.error("the walk failed on the drive")
    re.compile("the pattern is (here)")
    getattr(obj, "the name of the attr")
    os.environ.get("the variable that holds it")
    subprocess.run(["the command to run it"])
    con.execute("SELECT path FROM files WHERE the id = ?")
    raise TypeError("the value must be a string here")
__all__ = ["the names of it"]
FMT = "%Y-%m-%d %H:%M"
SQL = "SELECT path FROM files WHERE volume_id = ? AND the size > 0"
STAGES = "hash extract embed enrich"
'''
    check("docstrings, keys, subscripts, comparands, annotations, asserts, log, re, getenv, "
          "subprocess, SQL, programmer errors, __all__, formats and ids",
          read(src) == ["the drive to check against the target"], read(src))
    if sys.version_info >= (3, 10):
        check("match patterns", read('match k:\n    case "the drive is gone":\n        pass') == [])
    check("a run of one letter (yyyy) is a format field, not a word", not cb.prose("yyyy mm dd at"))


# The mutation check: house-rule before → after pairs, from bytecat's own words. Some
# befores are within budget (trips False): the house rules, not this guard, catch those.
# '' is a cut. A marker row passes under its ruling, and only there.
TABLE = [
    ("[bytecat] {name} failed; stopping. Progress is saved -- fix and re-run.", True,
     "[bytecat] {name} failed, pipeline stopped; re-run to resume."),
    ("[bytecat] {name} failed; pipeline stopped. Re-run to resume.", True,
     "[bytecat] {name} failed, pipeline stopped; re-run to resume."),
    ("Usually the interesting ones: a real backup, or a copy you forgot you made. "
     "Worth checking before the single-machine list.", True, ""),
    ("Both off-diagonal cells are empty: bytecat's verdict is confirmed by a fresh read "
     "of the disk under SHA-256.", True, "bytecat and the disk agree."),
    ("Nothing has been deleted or moved. This is a report; you decide.", True, ""),
    ("nothing was deleted or moved", False, ""),
    ("refusing non-loopback endpoint {host}. bytecat only talks to a local model; this is "
     "a hard invariant, not a default.", True, "refusing non-loopback model endpoint {host}"),
    ("no model server at {self.base}. Start one (LM Studio, Ollama, llama-server ...) or "
     "point BYTECAT_LLM_BASE at it.", True,
     "no model server or model at {self.base}; start one or set BYTECAT_LLM_BASE"),
    ("{url} unreachable after {retries} tries: {last}", False, "{url} unreachable: {last}"),
    ("bytecat and the disk agree on catalogued files. {uncompared} source file(s) not "
     "compared with the target.", True,
     "bytecat and the disk agree, but {uncompared} source file(s) were not compared."),
    ("**bytecat and the disk disagree.** Do not act on the coverage report until this is "
     "resolved.", True, "**bytecat and the disk disagree**: resolve before acting on the coverage report."),
    ("write a {MARKER} file at the volume root if it has no ID", True,
     "write {MARKER} at the volume root if it has no ID"),
    ("refusing non-loopback endpoint {host}. bytecat only talks to a local model; this is "
     "a hard invariant, not a default.", True,
     "refusing non-loopback model endpoint {host}. bytecat talks only to a local model.",
     'README Guarantees "Local only" - the refusal names the guarantee it enforces'),
]
AFTERS = [
    "Must be after {time}", "model server {e.code} on {path}: {detail}",
    "BYTECAT_CHAT_MODEL not set; pick one from {self.base}/models",
    "[bytecat] hashed {stats['full']}; reused {stats['adopted']}; "
    "{stats['skipped_unique']} need no read; {stats['errors']} unreadable",
    "**bytecat and the disk disagree**: duplicate reports on these groups are unsafe.",
    "_…and {n} more groups; `--top {len(groups)}` lists all._",
]


def _f(words):
    return "f" + json.dumps(words, ensure_ascii=False)


def _chain(words):
    cut = words.find(" ", len(words) // 2)
    return f"print({_f(words[:cut])} + {_f(words[cut:])})" if cut > 0 else f"print({_f(words)})"


SHAPES = [
    ("print", lambda w: f"print({_f(w)}, file=sys.stderr)"),
    ("a help= keyword", lambda w: f'ap.add_argument("--x", help={_f(w)})'),
    ("a raised error", lambda w: f"raise LMStudioDown({_f(w)})"),
    ("sys.exit", lambda w: f"sys.exit({_f(w)})"),
    ("a report line", lambda w: f"fh.write({_f(w + chr(10))})"),
    ("a const", lambda w: f"NOTE = {_f(w)}"),
    ("a + chain", _chain),
]


def part_a_mutation():
    print("a: the house rules, before and after (the mutation check)")
    for shape, write in SHAPES:
        bad = []
        for row in TABLE:
            before, trips, after = row[:3]
            marker = row[3] if len(row) > 3 else None
            if [o.text for o in judged(write(before)).offenders] != ([before] if trips else []):
                bad.append(f"before: {before}")
            if after and [o.text for o in judged(write(after)).offenders] != ([] if marker is None else [after]):
                bad.append(f"after: {after}")
            if marker is not None:
                j = judged(f"# copy-budget: {marker}\n{write(after)}")
                if (j.offenders, j.faults, len(j.exceptions)) != ([], [], 1):
                    bad.append(f"marked: {after}")
        bad += [f"after: {a}" for a in AFTERS if judged(write(a)).offenders]
        check(f"in {shape}, each before over budget trips it, and each after passes", not bad, "\n".join(bad))


def part_a_markers():
    print("a: exceptions")
    two = 'print("refusing non-loopback model endpoint. bytecat talks only to a local model.")'
    ok = judged(f'# copy-budget: README Guarantees "Local only" - names the guarantee\n{two}')
    quote = judged('# copy-budget: Alex 2026-10-11: "Every word and button needs to exist with intent and '
                   f'have good reason to remain." - names it\n{two}')
    check("a README Guarantees entry or a dated Alex quote, then the job, excuses the string",
          [(len(j.offenders), j.faults, len(j.exceptions)) for j in (ok, quote)] == [(0, [], 1)] * 2)
    bodies = ["because it reads better - names it", 'README Guarantees "Fast" - names it',
              'Alex 2026-02-30: "…" - names it', 'Alex 2999-01-01: "…" - names it',
              'README Guarantees "Local only" - ok']
    whys = [judged(f"# copy-budget: {b}\n{two}") for b in bodies]
    check("citing neither, an unknown guarantee, a non-day, a future day or no job fails, excusing nothing",
          all(len(j.faults) == 1 and len(j.offenders) == 1 and not j.exceptions for j in whys),
          "\n".join(f.why for j in whys for f in j.faults))
    within = judged('# copy-budget: README Guarantees "Local only" - names it\nprint("refusing non-loopback endpoint")')
    check("a marker above a string within budget fails: remove it",
          [f.why for f in within.faults] == [cb.WITHIN])
    gap = judged(f'# copy-budget: README Guarantees "Local only" - names it\n\n{two}')
    check("a marker counts only on the line directly above",
          [f.why for f in gap.faults] == [cb.NO_STRING] and len(gap.offenders) == 1)
    held = judged('# copy-budget: README Guarantees "Read-only" - the one fact the person acts on\n'
                  'ap.add_argument(\n    "--path",\n'
                  '    # copy-budget: README Guarantees "Read-only" - the same fact, at its keyword\n'
                  '    help="read-only. Nothing on the drive is moved or renamed.",\n)')
    check("a marker may sit above the keyword or assignment that holds the string",
          (len(held.offenders), len(held.exceptions), [f.why for f in held.faults]) == (0, 1, [cb.NO_STRING]))
    inside = scan('print("# copy-budget: README Guarantees \\"Local only\\" - names it")')
    check("a marker inside a string is words, not a marker", not inside.markers and len(inside.pieces) == 1)


def part_a_lists():
    print("a: model text, listed constants and leaks")
    src = ('SYSTEM = (\n    "You label files for a local search index. "\n'
           '    "Answer with one line: what the file is, then its topic."\n)\n')
    words = cb.words("You label files for a local search index. Answer with one line: what the file "
                     "is, then its topic.")
    pins = {FIXTURE: words}
    calm = judged(src, pins, model_text=("SYSTEM",))
    risen = judged(src, {FIXTURE: words - 1}, model_text=("SYSTEM",))
    check("model text is held to its pinned word count, not the cap",
          (calm.offenders, scan(src, model_text=("SYSTEM",)).pieces) == ([], []))
    check("a rise past the pin fails and names the pin",
          [cb.line(o) for o in risen.offenders] == [f"{FIXTURE}:2  model text {words} words, pinned at {words - 1}"])
    marked = judged('# copy-budget: README Guarantees "Local only" - the labeller stays local\n' + src,
                    {FIXTURE: words - 1}, model_text=("SYSTEM",))
    check("a rise passes under a marker citing a ruling, and a marker within the pin fails",
          (marked.offenders, len(marked.exceptions)) == ([], 1)
          and [f.why for f in judged('# copy-budget: README Guarantees "Local only" - stays local\n' + src,
                                     pins, model_text=("SYSTEM",)).faults] == [cb.WITHIN])
    stop = 'STOPWORDS = frozenset("""a an the of for from with to in on at by""".split())\n'
    s = scan(stop + "print(STOPWORDS)\nprint(SYSTEM)\nprint(BODY)\n",
             skip=("STOPWORDS",), model_text=("SYSTEM",), not_copy=("BODY",))
    check("a skip name holds no pieces; a model or skip name handed to print is a leak; not copy is not",
          (s.pieces, s.listed["STOPWORDS"], [m.body for m in s.leaks]) == ([], 1, ["STOPWORDS", "SYSTEM"]))


def part_a_markup():
    print("a: markup (the template's HTML and Jinja reader)")
    page = ('<h1>Duplicate report for {{ host }}</h1>\n<p>Nothing has been deleted or moved.\n'
            '   This is a report; you decide.</p>\n<p>hashed <b>{{ n }}</b> files {% if x %}on {{ host }}{% endif %}</p>\n'
            '<input placeholder="the drive to check against the target">\n'
            '<script>var s = "this is code, not words";</script>\n'
            '{# copy-budget: README Guarantees "Read-only" - the run changes nothing #}\n'
            '<!-- copy-budget: README Guarantees "Read-only" - the same, in HTML -->\n')
    s = cb.scan_markup("bytecat/fixture.html", page)
    check("text runs, inline tags, {{ x }} as one word, {% %} ending a run, drawn attributes; no script",
          [(p.line, p.text) for p in s.pieces] == [
              (1, "Duplicate report for {…}"),
              (2, "Nothing has been deleted or moved. This is a report; you decide."),
              (4, "hashed {…} files"), (4, "on {…}"), (5, "the drive to check against the target")],
          [(p.line, p.text) for p in s.pieces])
    check("markers from {# #} and <!-- -->, on their own lines", [m.line for m in s.markers] == [7, 8])


# ── b: every string bytecat prints keeps the budget ─────────────────────────

def part_b():
    print("b: the tree")
    files = sorted((ROOT / "bytecat").glob("*.py"))
    scans = []
    for f in files:
        rel = f.relative_to(ROOT).as_posix()
        scans.append(cb.scan_py(rel, f.read_text(encoding="utf-8"), CONFIG, not_copy=NOT_COPY.get(rel, ()),
                                model_text=MODEL_TEXT.get(rel, ((), 0))[0], skip=SKIP.get(rel, {})))
    names = guarantees((ROOT / "README.md").read_text(encoding="utf-8"))
    pins = {f: pin for f, (_, pin) in MODEL_TEXT.items()}
    j = cb.judge(scans, citer(names), pins)
    pieces = [p for s in scans for p in s.pieces]
    # 19 files and 229 pieces on 2026-10-15; the floors are well under.
    check("read the tree, and not an empty walk", len(files) > 15 and len(pieces) > 150, f"{len(files)} {len(pieces)}")
    check('README.md has its "Guarantees" entries', {"Local only", "Read-only"} <= names, sorted(names))
    check("every marker cites a ruling and sits on a string over budget", not j.faults,
          "\n".join(f"{f.file}:{f.line}  {f.why}  ({f.body})" for f in j.faults))
    check("no model-text or skip name is printed", not [m for s in scans for m in s.leaks],
          "\n".join(f"{m.file}:{m.line}  {m.body}" for s in scans for m in s.leaks))
    outside = [o for o in j.offenders if (o.file, o.text) not in PENDING]
    head = [f"{len(outside)} strings over budget: one sentence of at most {cb.BUDGET_WORDS} words, "
            "a {placeholder} being one.", SHORTEN,
            f"A ruling that needs the words goes on the line above: {MARKER_FORM}",
            "House rules: the header of tests/test_copy_budget.py.", ""]
    check(f"no string is over {cb.BUDGET_WORDS} words or one sentence without a marker", not outside,
          "\n".join(head + [cb.line(o) for o in outside]))
    found = {(o.file, o.text) for o in j.offenders}
    check("every PENDING entry is still over budget, and their count is pinned",
          set(PENDING) <= found and len(PENDING) == EXPECTED_PENDING,
          "\n".join(f"stale: {f}  {t}" for f, t in PENDING if (f, t) not in found))
    listed = {(s.file, n): s.listed[n] for s in scans
              for n in [*NOT_COPY.get(s.file, ()), *MODEL_TEXT.get(s.file, ((), 0))[0], *SKIP.get(s.file, {})]}
    counts = (sum(map(len, NOT_COPY.values())), sum(len(n) for n, _ in MODEL_TEXT.values()),
              sum(map(len, SKIP.values())))
    check("each listed constant still holds words, and each list's count is pinned",
          all(listed.values()) and len(listed) == sum(counts)
          and counts == (EXPECTED_NOT_COPY, EXPECTED_MODEL_TEXT, EXPECTED_SKIP),
          f"{counts} {[k for k, v in listed.items() if not v]}")
    totals = {s.file: sum(cb.words(p.text) for p in s.model) for s in scans if s.file in MODEL_TEXT}
    check("each model-text pin is its file's word count now", totals == pins,
          f"{totals} pinned {pins}: lower a pin when its count falls")
    check("every exception is counted in EXPECTED_EXCEPTIONS", len(j.exceptions) == EXPECTED_EXCEPTIONS,
          "\n".join(f"{m.file}:{m.line}  {m.body}" for m in j.exceptions))

    # A probe explainer on a real screen fails, a marker citing nothing fails with it,
    # and removing the probe returns to the baseline. Each is measured against the
    # tree as it stands, so a failure above does not hide one here.
    cli = next(s for s in scans if s.file == "bytecat/cli.py")
    source = (ROOT / cli.file).read_text(encoding="utf-8")
    probe = "[bytecat] pipeline complete. Every stage ran, and nothing on your drives was moved."
    m = re.search(r"^([ \t]*)print\(", source, re.M)
    probed = f'{m.group(1)}print("{probe}")\n'
    marked = f"{m.group(1)}# copy-budget: it reads better - reassures\n{probed}"

    def rejudge(text):
        s = cb.scan_py(cli.file, source[: m.start()] + text + source[m.start():], CONFIG)
        return cb.judge([x if x is not cli else s for x in scans], citer(names), pins)

    a, b, c = rejudge(probed), rejudge(marked), rejudge("")
    base = [cb.line(o) for o in j.offenders]
    probes = [o.text for o in a.offenders if o.text == probe]
    check("a probe explainer on a real screen fails",
          len(probes) == 1 + sum(o.text == probe for o in j.offenders) and len(a.offenders) == len(base) + 1)
    check("a marker that cites nothing fails, and excuses nothing",
          len(b.faults) == len(j.faults) + 1 and len(b.offenders) == len(base) + 1
          and len(b.exceptions) == len(j.exceptions))
    check("removing the probe returns to the baseline",
          [cb.line(o) for o in c.offenders] == base and c.faults == j.faults)
    return outside


if __name__ == "__main__":
    for part in (part_a_counting, part_a_read, part_a_skipped, part_a_mutation,
                 part_a_markers, part_a_lists, part_a_markup):
        part()
    outside = part_b()
    print(f"\noffenders {len(outside)} outside PENDING ({len(PENDING)} listed)")
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
