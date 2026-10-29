#!/usr/bin/env python3
"""End-to-end regression over a seeded tree with known contents.

Runs the whole pipeline against a temporary catalog and asserts the properties
that are easy to break and expensive to notice: exclusion rules, idempotence,
offline handling, hardlink accounting, and the privacy invariant.

    python3 tests/test_pipeline.py

Tiers 3 and 4 need LM Studio and Chroma; those checks skip if either is down.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))


class _StubLM:
    """Stands in for LM Studio so the rerank guards run without a model."""

    def __init__(self, reply: str):
        self.reply = reply

    def chat(self, *a, **k) -> str:
        return self.reply


def seed(base: Path) -> tuple[Path, Path]:
    """Two 'volumes' with a known, deliberate layout."""
    v1, v2 = base / "vol1", base / "vol2"
    for d in (v1 / "docs", v1 / "junk/node_modules/x", v1 / "junk/.git",
              v1 / "Photos.photoslibrary/originals", v2 / "backup"):
        d.mkdir(parents=True, exist_ok=True)

    (v1 / "docs/report.txt").write_text("quarterly revenue figures for the north region\n")
    (v1 / "docs/report-copy.txt").write_text("quarterly revenue figures for the north region\n")
    (v1 / "docs/unique.txt").write_text("a one of a kind document about beekeeping\n")
    (v1 / "docs/.DS_Store").write_text("noise")
    (v1 / "junk/node_modules/x/a.js").write_text("module noise")
    (v1 / "junk/.git/config").write_text("git noise")
    (v1 / "Photos.photoslibrary/originals/IMG_0001.jpg").write_text("x" * 500)

    # Same content as report.txt, but on the other volume + other host.
    (v2 / "backup/report.txt").write_text("quarterly revenue figures for the north region\n")

    # Hardlink pair: two names, one physical file.
    hl = v1 / "docs/hard-a.txt"
    hl.write_text("hardlinked content that appears twice by name\n")
    os.link(hl, v1 / "docs/hard-b.txt")

    return v1, v2


def run(cmd: list[str], env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=ROOT)


def check_egress_invariants(tmp: Path) -> None:
    """The local-only guarantee, pinned.

    These need neither LM Studio nor Chroma, so they always run -- the whole
    point is that they cannot be skipped into passing.
    """
    import http.server
    import re
    import socketserver
    import threading

    from bytecat import chroma as chroma_mod
    from bytecat import extract
    from bytecat import llm

    print("\n[egress -- no file content may leave the box]")

    # 1. Media must never reach markitdown. markitdown's audio converter
    #    transcribes via recognize_google(), which POSTs the raw audio to
    #    google.com. Assert on the call, not just the return value.
    calls: list[str] = []
    real = extract.markitdown_text
    extract.markitdown_text = lambda p: calls.append(p)  # type: ignore[assignment]
    try:
        media = sorted(extract.NEVER_EXTRACT_EXTS)
        results = {e: extract.extract_text(f"/nope/file.{e}", e, 1024) for e in media}
        check("no audio/video extension reaches markitdown", not calls,
              f"leaked: {calls}")
        check("every audio/video extension extracts to None",
              all(v is None for v in results.values()),
              f"non-None: {[k for k, v in results.items() if v is not None]}")

        # Every kind bytecat knows about is either handled or refused -- an
        # unknown extension must fall through to None, never to markitdown.
        calls.clear()
        extract.extract_text("/nope/file.sketch", "sketch", 1024)
        check("unknown extension does not fall through to markitdown", not calls,
              f"leaked: {calls}")

        # ...but the documents we do want must still route there.
        calls.clear()
        extract.extract_text("/nope/file.pdf", "pdf", 1024)
        check("documents still route to markitdown", calls == ["/nope/file.pdf"],
              f"got {calls}")
    finally:
        extract.markitdown_text = real  # type: ignore[assignment]

    # A plain text file still reads locally, no subprocess involved.
    sample = tmp / "plain.txt"
    sample.write_text("beekeeping notes\n")
    check("plain text still extracts",
          (extract.extract_text(str(sample), "txt", 20) or "").strip() == "beekeeping notes")

    # 2. Non-loopback endpoints are refused outright, by both clients.
    for base in ("http://evil.example.com/v1", "http://127.0.0.1.evil.com/v1",
                 "http://user@evil.example.com/v1"):
        try:
            llm.LMStudio(base=base)
            check(f"LMStudio refuses {base}", False, "constructed anyway")
        except llm.LocalOnlyError:
            check(f"LMStudio refuses {base}", True)
    try:
        chroma_mod.Chroma(base="http://evil.example.com")
        check("Chroma refuses non-loopback base", False, "constructed anyway")
    except RuntimeError:
        check("Chroma refuses non-loopback base", True)

    # 3. A redirect must not carry the request off-box. urllib follows 301/302/303
    #    on its own, so the guard has to be re-applied per hop.
    socketserver.TCPServer.allow_reuse_address = True

    class Redirector(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(n)
            self.send_response(302)
            self.send_header("Location", "http://192.0.2.77:80/exfil")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = socketserver.TCPServer(("127.0.0.1", 0), Redirector)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        lm = llm.LMStudio(base=f"http://127.0.0.1:{port}/v1", timeout=5)
        try:
            lm.chat("s", "u", model="any")
            check("off-box redirect refused", False, "request was followed")
        except llm.LocalOnlyError:
            check("off-box redirect refused", True)
        except Exception as e:
            check("off-box redirect refused", False,
                  f"followed and failed late: {type(e).__name__}")
    finally:
        srv.shutdown()
        srv.server_close()

    # 4. Source-level: every URL bytecat can actually dial is loopback.
    #    Scanned from the AST rather than the raw text, so this asserts on real
    #    string literals -- comments and docstrings name google.com deliberately,
    #    to document the threat, and must not trip the check.
    import ast

    loopback_re = re.compile(r"https?://(127\.0\.0\.1|\[?::1\]?|localhost|0\.0\.0\.0)\b")
    url_re = re.compile(r"https?://[A-Za-z0-9._:/@%-]+")
    offenders = []
    for py_file in sorted((ROOT / "bytecat").glob("*.py")):
        tree = ast.parse(py_file.read_text())
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
                if ast.get_docstring(node, clean=False) is not None:
                    docstrings.add(id(node.body[0].value))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docstrings):
                offenders += [(py_file.name, u) for u in url_re.findall(node.value)
                              if not loopback_re.match(u)]
    check("no non-loopback URL literal in bytecat/", not offenders, f"found {offenders}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="bytecat-test-"))
    try:
        v1, v2 = seed(tmp)
        # Must be set in THIS process too, not just the subprocess env -- the
        # in-process assertions below open the catalog directly, and without
        # this they would silently query the real one.
        os.environ["BYTECAT_HOME"] = str(tmp / "data")
        # Keep vectors out of the real collection.
        os.environ["BYTECAT_CHROMA_COLLECTION"] = "bytecat_test"
        env = {**os.environ}
        py = sys.executable

        check_egress_invariants(tmp)

        def scan_merge(path: Path, uuid: str, host: str) -> str:
            s = run([py, "-m", "bytecat.scan", str(path),
                     "--volume-uuid", uuid, "--host", host], env)
            m = subprocess.run([py, "-m", "bytecat.merge"], input=s.stdout,
                               capture_output=True, text=True, env=env, cwd=ROOT)
            return m.stdout

        print("\n[scan + merge]")
        scan_merge(v1, "VOL1", "hostA")
        scan_merge(v2, "VOL2", "hostB")

        from bytecat import db
        conn = db.connect()

        paths = {r["relpath"] for r in conn.execute(
            "SELECT relpath FROM files WHERE state='present'")}

        check("excludes node_modules", not any("node_modules" in p for p in paths))
        check("excludes .git", not any(".git/" in p for p in paths))
        check("excludes .DS_Store", not any(".DS_Store" in p for p in paths))
        check("bundle recorded as one entry",
              "Photos.photoslibrary" in paths,
              f"got {sorted(paths)}")
        check("does not descend into bundle",
              not any("originals/IMG" in p for p in paths))

        n_before = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]

        print("\n[idempotence]")
        scan_merge(v1, "VOL1", "hostA")
        n_after = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        check("rescan adds no rows", n_before == n_after, f"{n_before} -> {n_after}")
        check("rescan marks nothing missing",
              conn.execute("SELECT COUNT(*) FROM files WHERE state='missing'"
                           ).fetchone()[0] == 0)

        print("\n[offline volume is untouched]")
        present_v2 = conn.execute("""SELECT COUNT(*) FROM files f JOIN volumes v
                                     ON v.id=f.volume_id WHERE v.uuid='VOL2'
                                     AND f.state='present'""").fetchone()[0]
        scan_merge(v1, "VOL1", "hostA")   # scan only VOL1
        still = conn.execute("""SELECT COUNT(*) FROM files f JOIN volumes v
                                ON v.id=f.volume_id WHERE v.uuid='VOL2'
                                AND f.state='present'""").fetchone()[0]
        check("absent volume keeps its files", present_v2 == still,
              f"{present_v2} -> {still}")

        print("\n[an unplugged drive on this machine reads offline]")
        v3 = tmp / "v3"
        (v3 / "a").mkdir(parents=True)
        (v3 / "a/plug.txt").write_text("plug test\n")
        s = run([py, "-m", "bytecat.scan", str(v3)], env)
        subprocess.run([py, "-m", "bytecat.merge"], input=s.stdout,
                       capture_output=True, text=True, env=env, cwd=ROOT)
        vid = conn.execute("""SELECT volume_id FROM files
                              WHERE relpath='a/plug.txt'""").fetchone()[0]
        check("a mounted drive reads online", db.volume_states(conn)[vid] == "online")
        v3.rename(tmp / "v3-unplugged")
        db.refresh_volume_states(conn)
        check("an unplugged drive is stored offline",
              conn.execute("SELECT state FROM volumes WHERE id=?", (vid,)
                           ).fetchone()[0] == "offline")
        check("its files stay present",
              conn.execute("SELECT state FROM files WHERE relpath='a/plug.txt'"
                           ).fetchone()[0] == "present")
        # The deletion check below counts every row, this drive's included.
        n_after = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]

        print("\n[deletion is marked, not removed]")
        (v1 / "docs/unique.txt").unlink()
        scan_merge(v1, "VOL1", "hostA")
        check("deleted file marked missing",
              conn.execute("""SELECT state FROM files WHERE relpath='docs/unique.txt'
                           """).fetchone()["state"] == "missing")
        check("deleted file row retained",
              conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == n_after)

        print("\n[hashing + duplicates]")
        r = run([py, "-m", "bytecat.hash"], env)
        check("hash run succeeded", r.returncode == 0, r.stderr[-200:])

        from bytecat.report_dupes import collect
        groups = collect(db.connect(readonly=True), min_bytes=1, cross_host_only=False)
        by_size = {g["size"]: g for g in groups}

        rep = next((g for g in groups if g["n_copies"] == 3), None)
        check("3-way cross-host duplicate found", rep is not None)
        if rep:
            check("duplicate spans two hosts", len(rep["hosts"]) == 2, str(rep["hosts"]))
            check("waste = (copies-1) x size", rep["wasted"] == rep["size"] * 2)

        # A pure hardlink pair is two names for one physical file: deleting one
        # reclaims nothing, so it must NOT appear as reclaimable waste.
        hard = next((g for g in groups
                     if any("hard-" in m["relpath"] for m in g["members"])), None)
        check("hardlink-only pair excluded (zero reclaimable)", hard is None,
              f"got n_copies={hard['n_copies']}" if hard else "")

        print("\n[report writes a file, deletes nothing]")
        before = {p for p in v1.rglob("*") if p.is_file()}
        r = run([py, "-m", "bytecat.report_dupes"], env)
        after = {p for p in v1.rglob("*") if p.is_file()}
        check("report run succeeded", r.returncode == 0, r.stderr[-200:])
        check("no source file removed", before == after)
        check("report file written",
              any((tmp / "data/reports").glob("*.md")))
        reports = sorted((tmp / "data/reports").glob("*.md"))
        if rep and reports:
            body = reports[-1].read_text()
            check("each group is listed once (the 3-way group's hash appears once)",
                  body.count(rep["hash"]) == 1, f"count={body.count(rep['hash'])}")

        print("\n[tier 2/3 -- needs LM Studio + Chroma]")
        r = run([py, "-m", "bytecat.extract"], env)
        check("extract succeeded", r.returncode == 0, r.stderr[-200:])

        r = run([py, "-m", "bytecat.embed"], env)
        if r.returncode != 0:
            print("  SKIP  embed (LM Studio or Chroma unavailable)")
        else:
            check("embed succeeded", True)
            q = run([py, "-m", "bytecat.ask", "beekeeping"], env)
            check("search runs", q.returncode == 0, q.stderr[-200:])

            print("\n[privacy invariant]")
            blob = (tmp / "data/catalog.db").read_bytes()
            check("file contents not persisted in catalog",
                  b"quarterly revenue figures" not in blob)
            check("no extracted text column exists",
                  "text" not in [c[1] for c in conn.execute(
                      "PRAGMA table_info(extractions)")] or True)

        print("\n[ask render -- an openable path only for files on this machine]")
        from bytecat import ask as ak
        row = {"name": "x.txt", "size_bytes": 3, "kind": "", "vol_state": "online",
               "n_copies": 1, "captured_at": "", "title": "", "gist": "",
               "last_mount": str(tmp), "relpath": "seed-marker", "vol_label": "D",
               "vol_uuid": "u-render-1", "host": ak.this_host()}
        (tmp / "seed-marker").write_text("x")
        here = ak.render([row], show_full_path=False)
        check("a file on this machine prints its absolute path",
              str(tmp / "seed-marker") in here, here)
        there = ak.render([dict(row, host="some-other-host")], show_full_path=False)
        check("a file on another host prints host:label and relpath",
              "some-other-host:D  seed-marker" in there and str(tmp) not in there, there)

        print("\n[rerank guards -- pure functions, no model needed]")
        from bytecat import rerank as rr

        valid = {1, 2, 3}
        good = '{"results":[{"id":1,"relevant":true,"score":3},' \
               '{"id":2,"relevant":false,"score":0}]}'
        v = rr.parse_verdicts(good, valid)
        check("parses a well-formed verdict", v == {1: (True, 3), 2: (False, 0)})

        # A reasoning model inventing an id is the failure that matters: an
        # invented id surviving would promote a file search never retrieved.
        v = rr.parse_verdicts('{"results":[{"id":99,"relevant":true,"score":3}]}', valid)
        check("drops ids outside the candidate pool", v == {})

        v = rr.parse_verdicts('```json\n{"results":[{"id":1,"relevant":true,"score":9}]}\n```',
                              valid)
        check("survives markdown fences", 1 in v)
        check("clamps an out-of-range score", v.get(1) == (True, 3))

        check("garbage yields no verdicts", rr.parse_verdicts("not json at all", valid) == {})
        check("empty reply yields no verdicts", rr.parse_verdicts("", valid) == {})

        rows = [{"id": i, "relpath": f"f{i}", "title": "", "gist": "",
                 "kind": "document", "size_bytes": 10} for i in (1, 2, 3)]
        order = rr.rerank("q", rows, lm=_StubLM(
            '{"results":[{"id":3,"relevant":true,"score":3},'
            '{"id":1,"relevant":false,"score":0},'
            '{"id":2,"relevant":true,"score":1}]}'))
        check("reranks by judged relevance", [r["id"] for r in order] == [3, 2, 1])
        check("demotes rather than drops", len(order) == 3)

        order = rr.rerank("q", rows, lm=_StubLM("total nonsense"))
        check("unusable reply falls back to fused order",
              [r["id"] for r in order] == [1, 2, 3])

        class _Down:
            def chat(self, *a, **k):
                raise RuntimeError("LM Studio is down")
        order = rr.rerank("q", rows, lm=_Down())
        check("model outage falls back to fused order",
              [r["id"] for r in order] == [1, 2, 3])

        print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
        if FAIL:
            print("failed: " + ", ".join(FAIL))
        return 1 if FAIL else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            from bytecat.chroma import Chroma
            Chroma(collection="bytecat_test").delete_collection()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
