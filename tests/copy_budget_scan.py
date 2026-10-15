"""The copy-budget guard's scanner: source text in, the pieces of copy a person
reads out, each judged against one sentence of at most 12 words. The rules,
the repo's config and the tree walk live in test_copy_budget.py.

It is a template, copied into each repo and never imported across repos;
TEMPLATE below is its provenance line, so `diff` shows drift. Standard
library only, Python 3.9 and later. It takes text, never paths.

Counting. A word is a whitespace token with a letter or a digit in it, and a
placeholder ({name}, {…}, {}) is one word; "·" and "›" are not words. A
sentence ends at . ! or ? followed by a space and more words, a closing quote,
bracket or Markdown ** or _ between them, so "e.g.", "i.e.", "7 a.m.",
"07:00", "1.5", "..." mid-string and a trailing period end none.

What is read. Every str literal and f-string, each {…} hole being one word; a
+ chain as one string; implicit concatenation is one literal already. Copy
whatever it looks like: positional arguments to Config.always_calls (print,
an error a person sees) and values of Config.copy_keys (help=, description=).
Any other literal counts only when it is prose: at least two of its words, and
at least half, are plain words (a run of one letter, yyyy, is a format field),
and one of them is a function word, so lists of extensions or ids are left
out. A module docstring is read when the module passes __doc__ on as help.

What is skipped. Docstrings and other bare strings, dict keys, subscripts,
comparands, annotations, match patterns, __all__, format specs and assert
messages: keys, not words. Arguments to Config.skip_calls (log lines, re,
getattr, getenv, subprocess, SQL) and to Config.programmer_errors, which only
a developer reads. Exceptions are not skipped wholesale: in Python, domain
errors reach the person.

Listed constants. The test names, per file, three kinds of constant the cap
does not judge: not copy (content the person asked to read, held to truth and
plainness instead of one line), model text (it steers a model, so it is held
to a pinned word count per file, which may fall and may rise only under a
marker) and skip (non-prose a first run flags, such as a stopword list). A
model-text or skip name handed to an always-read call is a leak: copy after all.

Markers. `# copy-budget: <ruling> - <the job these words do>` on the line
directly above the string, or above what holds it (the keyword, assignment,
dict key or call). In a template, `{# copy-budget: … #}` or
`<!-- copy-budget: … -->`. Markers come from tokenize, so marker text inside
a string is never a marker.
"""
from __future__ import annotations

import ast
import datetime
import html.parser
import io
import re
import tokenize
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Optional

TEMPLATE = "copy-budget python template, version 1 (2026-10-15)"

BUDGET_WORDS = 12

_ALNUM = re.compile(r"[^\W_]")
_HOLE = re.compile(r"\{[^{}\s]*\}")
_L = r"[^\W\d_]"
_PLAIN = re.compile(rf"^{_L}+(?:['’-]{_L}+)*$|^{_L}(?:\.{_L})+$")
_FIELD = re.compile(rf"^({_L})\1+$")
_BARE = re.compile(r"^[(“\"'‘«]+|[)”\"'’»,.;:!?…]+$")
_END = re.compile(r"([.!?]+)[\"'”’)\]*_]*\s+")
_ABBREVIATION = re.compile(r"(?:^|[\s(])(?:e\.g|i\.e|a\.m|p\.m|etc|vs|approx|incl|excl|cf)\.$", re.I)
_SQL = re.compile(
    r"^\s*(?:SELECT|INSERT|UPDATE|DELETE|WITH|CREATE|ALTER|DROP|PRAGMA)\b[\s\S]*"
    r"\b(?:FROM|INTO|SET|WHERE|VALUES|TABLE|JOIN|RETURNING)\b"
)
FUNCTION_WORDS = frozenset(
    "a an the and or but nor not no is are was were be been being am do does did has have had "
    "it its this that these those there here then than so if when where while which who what why "
    "how all any each every some none only also just still yet already never again to of in on at "
    "by for from with without into onto out over under up down off about after before since until "
    "via per as you your we our us they them their he she his her me my can cannot could should "
    "would will may might must shall can't won't isn't aren't wasn't don't doesn't didn't hasn't "
    "haven't couldn't shouldn't wouldn't".split()
)
_MARKER = re.compile(r"^#\s*copy-budget:(.*)$")
_QUOTE = re.compile(r"^Alex (\d{4})-(\d\d)-(\d\d): (?:\"[^\"]+\"|“[^”]+”)\s+-{1,2}\s+(.+)$")
_JOB = re.compile(rf"{_L}{{3}}")
_MATCH_CASE = getattr(ast, "match_case", ())


def _tokens(text: str) -> list:
    return text.split()


def _is_word(token: str) -> bool:
    return bool(_ALNUM.search(token) or _HOLE.search(token))


def _plain_words(text: str) -> list:
    bare = (_BARE.sub("", t) for t in _tokens(text))
    return [b for b in bare if _PLAIN.match(b) and not _FIELD.match(b)]


def words(text: str) -> int:
    """Words in `text`: tokens with a letter or digit in them, each placeholder one."""
    return sum(1 for t in _tokens(text) if _is_word(t))


def sentences(text: str) -> int:
    """Sentences in `text`: each . ! or ? followed by a space and more words closes one."""
    count, start = 0, 0
    for end in _END.finditer(text):
        mark = end.group(1)
        following = _tokens(text[end.end():])
        if not following or not _is_word(following[0]):
            continue
        if re.fullmatch(r"\.{2,}", mark) or _ABBREVIATION.search(text[: end.start() + len(mark)]):
            continue
        if words(text[start : end.start()]) > 0:
            count += 1
        start = end.end()
    return count + (1 if words(text[start:]) > 0 else 0)


def over(text: str) -> bool:
    """More than one sentence, or more than 12 words."""
    return words(text) > BUDGET_WORDS or sentences(text) > 1


def prose(text: str) -> bool:
    plain = _plain_words(text)
    return (
        not _SQL.match(text)
        and len(plain) >= 2
        and len(plain) * 2 >= words(text)
        and any(w.lower() in FUNCTION_WORDS for w in plain)
    )


@dataclass(frozen=True)
class Piece:
    """One string as a person reads it. A marker may sit on the line above any anchor."""

    file: str
    line: int
    anchors: tuple
    text: str
    owner: str = ""


@dataclass(frozen=True)
class Marker:
    file: str
    line: int
    body: str


@dataclass
class Scan:
    file: str
    pieces: list = field(default_factory=list)
    markers: list = field(default_factory=list)
    model: list = field(default_factory=list)
    listed: Counter = field(default_factory=Counter)
    leaks: list = field(default_factory=list)


@dataclass(frozen=True)
class Offender:
    file: str
    line: int
    words: int
    sentences: int
    text: str
    pin: Optional[int] = None


@dataclass(frozen=True)
class Fault:
    file: str
    line: int
    body: str
    why: str


@dataclass
class Judgement:
    offenders: list
    faults: list
    exceptions: list


def _callee_matcher(names) -> Callable[[str], bool]:
    """A callee matches an entry that is a whole run of its dotted parts: "re" matches
    re.sub, "write" matches fh.write, "os.environ" matches os.environ.get."""
    if not names:
        return lambda name: False
    pattern = re.compile(r"(?:^|\.)(?:" + "|".join(map(re.escape, sorted(names))) + r")(?:\.|$)")
    return lambda name: bool(name) and pattern.search(name) is not None


@dataclass(frozen=True)
class Config:
    """What one repo reads and skips. Each repo's test fills this in."""

    always_calls: frozenset = frozenset({"print"})
    copy_keys: frozenset = frozenset({"help", "description", "epilog", "usage"})
    skip_calls: frozenset = frozenset(
        {"logging", "logger", "log", "warnings.warn", "re", "getattr", "setattr", "hasattr",
         "getenv", "os.environ", "subprocess", "execute", "executemany", "executescript"}
    )
    programmer_errors: frozenset = frozenset(
        {"AssertionError", "TypeError", "KeyError", "IndexError", "AttributeError", "NotImplementedError"}
    )


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _dotted(node.value) + "." + node.attr
    if isinstance(node, ast.Call):
        return _dotted(node.func)
    return ""


def _target(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _hole(expr: ast.AST) -> str:
    text = ast.unparse(expr)
    return "{" + text + "}" if re.fullmatch(r"[\w.()\[\]'\"]{1,40}", text) else "{…}"


def _literal(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value if isinstance(v, ast.Constant) else _hole(v.value) for v in node.values
        )
    return None


def _is_plus(node) -> bool:
    return isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)


def _passes_through(p: ast.AST, n: ast.AST) -> bool:
    """Whether `p` hands the value `n` on unchanged, so what holds `p` holds `n`."""
    if isinstance(p, ast.IfExp):
        return n is not p.test
    if isinstance(p, ast.BinOp):
        return isinstance(p.op, ast.Add) or (isinstance(p.op, ast.Mod) and n is p.left)
    if isinstance(p, ast.Attribute):
        return n is p.value and p.attr == "format"
    if isinstance(p, ast.Call):
        return n is p.func and isinstance(n, ast.Attribute) and n.attr == "format"
    if isinstance(p, ast.NamedExpr):
        return n is p.value
    if isinstance(p, ast.FormattedValue):
        return n is p.value
    return isinstance(p, (ast.BoolOp, ast.JoinedStr, ast.List, ast.Tuple, ast.Set, ast.Starred))


def scan_py(file: str, source: str, cfg: Config = Config(), not_copy=(), model_text=(), skip=()) -> Scan:
    """Every piece of copy in one Python file, and every copy-budget marker. The three
    lists name this file's listed constants; scan.listed counts the strings each holds."""
    tree = ast.parse(source, filename=file)
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    always_call = _callee_matcher(cfg.always_calls)
    skip_call = _callee_matcher(cfg.skip_calls)
    listed = frozenset(not_copy) | frozenset(model_text) | frozenset(skip)
    hidden = frozenset(model_text) | frozenset(skip)
    scan = Scan(file)
    taken = set()

    def ancestors(node):
        n, p = node, parents.get(node)
        while p is not None:
            yield n, p
            n, p = p, parents.get(p)

    def skipped(node) -> bool:
        for n, p in ancestors(node):
            if isinstance(p, ast.Call) and n is not p.func:
                name = _dotted(p.func)
                if skip_call(name) or name.rsplit(".", 1)[-1] in cfg.programmer_errors:
                    return True
            if isinstance(p, ast.Assert) or (isinstance(p, ast.Subscript) and n is p.slice):
                return True
            if isinstance(p, (ast.arg, ast.AnnAssign)) and n is p.annotation:
                return True
            if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is p.returns:
                return True
            if isinstance(p, ast.FormattedValue) and n is p.format_spec:
                return True
            if _MATCH_CASE and isinstance(p, _MATCH_CASE) and n is p.pattern:
                return True
            if isinstance(p, ast.Assign) and any(_target(t) == "__all__" for t in p.targets):
                return True
        return False

    def holder(node):
        """(always read, the holder's line), or None when the string is a key, comparand or docstring."""
        n, p = node, parents.get(node)
        while p is not None and _passes_through(p, n):
            n, p = p, parents.get(p)
        if p is None or isinstance(p, (ast.Expr, ast.Compare)):
            return None
        if isinstance(p, ast.Dict):
            if any(k is n for k in p.keys):
                return None
            key = p.keys[[v is n for v in p.values].index(True)]
            name = key.value if isinstance(key, ast.Constant) else ""
            return name in cfg.copy_keys, getattr(key, "lineno", None)
        if isinstance(p, ast.keyword):
            return p.arg in cfg.copy_keys, p.lineno
        if isinstance(p, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            return False, p.lineno
        if isinstance(p, ast.Call) and any(a is n for a in p.args):
            return always_call(_dotted(p.func)), p.lineno
        return False, None

    def owner(node) -> str:
        for n, p in ancestors(node):
            if isinstance(p, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and n is p.value:
                targets = p.targets if isinstance(p, ast.Assign) else [p.target]
                return _target(targets[0])
            if isinstance(p, ast.stmt):
                return ""
        return ""

    def add(node, text: str) -> None:
        if not _plain_words(text):
            return
        name, line, held = owner(node), node.lineno, holder(node)
        at = None if held is None else held[1]
        piece = Piece(file, line, (line,) if at in (None, line) else (line, at), " ".join(text.split()), name)
        if name in listed:
            scan.listed[name] += 1
            if name in model_text:
                scan.model.append(piece)
        elif held is not None and (held[0] or prose(text)) and not skipped(node):
            scan.pieces.append(piece)

    def read_chain(root) -> None:
        parts, stack = [], [root]
        while stack:
            n = stack.pop()
            if _is_plus(n):
                stack.extend([n.right, n.left])
            else:
                parts.append(n)
        texts = [_literal(p) for p in parts]
        if all(t is None for t in texts):
            return
        taken.update(p for p, t in zip(parts, texts) if t is not None)
        add(root, "".join(t if t is not None else _hole(p) for p, t in zip(parts, texts)))

    def visit(node) -> None:
        if node not in taken:
            text = _literal(node)
            if text is not None:
                add(node, text)
            elif _is_plus(node) and not _is_plus(parents.get(node)):
                read_chain(node)
        if isinstance(node, ast.JoinedStr):
            for v in node.values:
                if isinstance(v, ast.FormattedValue):
                    visit(v.value)
            return
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    doc = tree.body[0] if tree.body and isinstance(tree.body[0], ast.Expr) else None
    if doc is not None and _literal(doc.value) is not None:
        if any(isinstance(n, ast.Name) and n.id == "__doc__" for n in ast.walk(tree)):
            line = doc.value.lineno
            scan.pieces.append(Piece(file, line, (line,), " ".join(doc.value.value.split())))

    # A model-text or skip name handed to print, help or an error is copy after all.
    # Not copy is the person's own content, so handing it to print is its job.
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in hidden:
            held = holder(node)
            if held is not None and held[0] and not skipped(node):
                scan.leaks.append(Marker(file, node.lineno, node.id))

    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT and tok.line.lstrip().startswith("#"):
            m = _MARKER.match(tok.string)
            if m:
                scan.markers.append(Marker(file, tok.start[0], m.group(1).strip()))
    return scan


_INLINE = frozenset("a abbr b bdi bdo br cite code data dfn em i kbd mark q s samp small span strong sub sup time u var wbr".split())
_READ_ATTRS = frozenset({"title", "placeholder"})
_JINJA = re.compile(r"\{\{.*?\}\}|\{%.*?%\}|\{#(.*?)#\}", re.S)


class _Markup(html.parser.HTMLParser):
    def __init__(self, file: str) -> None:
        super().__init__(convert_charrefs=True)
        self.file, self.pieces, self.run, self.at, self.quiet = file, [], "", 0, 0

    def flush(self) -> None:
        if _plain_words(self.run):
            self.pieces.append(Piece(self.file, self.at, (self.at,), " ".join(self.run.split())))
        self.run = ""

    def handle_starttag(self, tag, attrs) -> None:
        if tag in ("script", "style"):
            self.quiet += 1
        if tag not in _INLINE:
            self.flush()
        line = self.getpos()[0]
        for name, value in attrs:
            if name in _READ_ATTRS and value and _plain_words(value):
                self.pieces.append(Piece(self.file, line, (line,), " ".join(value.split())))

    def handle_endtag(self, tag) -> None:
        if tag in ("script", "style"):
            self.quiet = max(0, self.quiet - 1)
        if tag not in _INLINE:
            self.flush()

    def handle_data(self, data) -> None:
        if self.quiet:
            return
        first, offset = self.getpos()[0], 0
        for i, part in enumerate(data.split("\0")):
            if i:
                self.flush()
            if not self.run.strip() and part.strip():
                self.at = first + data.count("\n", 0, offset + len(part) - len(part.lstrip()))
            self.run += part
            offset += len(part) + 1


def scan_markup(file: str, source: str) -> Scan:
    """Text and drawn attributes in an HTML or Jinja template, and its copy-budget markers."""
    markers = []

    def swap(m: re.Match) -> str:
        line = source.count("\n", 0, m.start()) + 1
        if m.group(1) is not None:
            body = re.match(r"\s*copy-budget:(.*)$", m.group(1), re.S)
            if body:
                markers.append(Marker(file, line, " ".join(body.group(1).split())))
        newlines = "\n" * m.group(0).count("\n")
        # {{ x }} is one word; {% … %} and {# … #} end the run they sit in.
        return "{…}" + newlines if m.group(0).startswith("{{") else "\0" + newlines

    parser = _Markup(file)
    parser.feed(_JINJA.sub(swap, source))
    parser.close()
    parser.flush()
    for m in re.finditer(r"<!--\s*copy-budget:(.*?)-->", source, re.S):
        markers.append(Marker(file, source.count("\n", 0, m.start()) + 1, " ".join(m.group(1).split())))
    return Scan(file, parser.pieces, markers)


def has_job(text: str) -> bool:
    return bool(_JOB.search(text))


def quote_citation(body: str, other: str) -> Optional[str]:
    """Why a dated Alex quote does not hold, or None when it does. `other` names the repo's other form."""
    m = _QUOTE.match(body)
    if m is None:
        return f'cites neither {other} nor a dated Alex quote (Alex 2026-10-11: "…"), then " - " and the job'
    try:
        day = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return f"dates the quote {m.group(1)}-{m.group(2)}-{m.group(3)}, which is not a day"
    if day > datetime.date.today():
        return f"dates the quote {day}, which has not happened yet"
    return None if has_job(m.group(4)) else "names no job for the words"


NO_STRING = "no string starts on the line below it; a marker sits directly above the string it excuses"
WITHIN = "the string below it is within budget now; remove the marker"


def judge(scans, cite: Callable[[str], Optional[str]], pins=None) -> Judgement:
    """A string over budget is an offender unless a valid marker sits above it; so is a
    file's model text over its pinned words. A marker that cites nothing, covers no
    string, or covers only strings within budget is a fault; each valid one is an exception."""
    pins = pins or {}
    offenders, faults, exceptions = [], [], []
    for scan in scans:
        total = sum(words(p.text) for p in scan.model)
        risen = bool(scan.model) and total > pins.get(scan.file, 0)
        excused, model_excused = set(), False
        for m in scan.markers:
            below = [p for p in scan.pieces if m.line + 1 in p.anchors]
            model = [p for p in scan.model if m.line + 1 in p.anchors]
            heavy = [p for p in below if over(p.text)]
            why = cite(m.body) or (
                NO_STRING if not below and not model else WITHIN if not heavy and not (model and risen) else None
            )
            if why is not None:
                faults.append(Fault(m.file, m.line, m.body, why))
                continue
            exceptions.append(m)
            excused.update(heavy)
            model_excused = model_excused or bool(model)
        for p in scan.pieces:
            if p not in excused and over(p.text):
                offenders.append(Offender(p.file, p.line, words(p.text), sentences(p.text), p.text))
        if risen and not model_excused:
            first = scan.model[0]
            offenders.append(Offender(scan.file, first.line, total, 0, "model text", pins.get(scan.file, 0)))
    return Judgement(offenders, faults, exceptions)


def line(o: Offender) -> str:
    """One failure line a person can act on: where, how long, and the words."""
    if o.pin is not None:
        return f"{o.file}:{o.line}  model text {o.words} words, pinned at {o.pin}"
    noun = "sentence" if o.sentences == 1 else "sentences"
    return f'{o.file}:{o.line}  {o.words} words, {o.sentences} {noun}  "{o.text}"'
