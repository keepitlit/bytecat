#!/usr/bin/env python3
"""embed --reset clears only what the same run can rebuild.

It used to drop the whole Chroma collection and every t3 flag before it
checked the model server: with the server down nothing came back, and the
vectors of another host's drive or an unplugged one could not come back here
at all. In-process, with a fake Chroma and a fake model server; no network.
"""
import contextlib, io, os, shutil, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PASS, FAIL = [], []

def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))


STORE: dict[str, dict] = {}


class FakeChroma:
    base = "http://127.0.0.1:0"

    def __init__(self, *a, **k):
        pass

    def heartbeat(self):
        return True

    def collection_id(self):
        return "c"

    def upsert(self, ids, embeddings, metadatas, documents=None):
        for i, m in zip(ids, metadatas):
            STORE[i] = m

    def ids(self, page=1000):
        return list(STORE)

    def delete(self, ids, page=1000):
        for i in ids:
            STORE.pop(i, None)

    def delete_collection(self):
        STORE.clear()


class FakeLM:
    up = True

    def __init__(self, *a, **k):
        pass

    def ensure_up(self, **k):
        from bytecat.llm import ModelServerDown
        if not FakeLM.up:
            raise ModelServerDown("no model server or model at fake; start one")
        return ["m"]

    def embed(self, texts, model=""):
        return [[0.0, 0.0, 1.0] for _ in texts]


tmp = Path(tempfile.mkdtemp(prefix="bytecat-reset-"))
try:
    os.environ["BYTECAT_HOME"] = str(tmp / "home")
    os.environ["BYTECAT_HOST"] = "here"
    env = dict(os.environ)
    for name, host, files in (("mine", "here", ("a.txt", "b.jpg")),
                              ("theirs", "elsewhere", ("c.txt",))):
        d = tmp / name
        d.mkdir()
        for f in files:
            (d / f).write_text(f"{name} {f}\n")
        args = ["--host", host] if host != "here" else []
        s = subprocess.run([sys.executable, "-m", "bytecat.scan", str(d), "--volume-uuid",
                            f"U-{name}", *args], cwd=ROOT, env=env,
                           capture_output=True, text=True)
        subprocess.run([sys.executable, "-m", "bytecat.merge"], cwd=ROOT, env=env,
                       input=s.stdout, capture_output=True, text=True)
    subprocess.run([sys.executable, "-m", "bytecat.extract"], cwd=ROOT, env=env,
                   capture_output=True, text=True)

    from bytecat import db, embed
    embed.Chroma = FakeChroma
    embed.ModelServer = FakeLM
    conn = db.connect()
    ids = {r["name"]: str(r["id"]) for r in conn.execute("SELECT id, name FROM files")}
    check("three files catalogued", len(ids) == 3, str(ids))

    def seed():
        """Every file embedded, plus one vector whose file was forgotten."""
        STORE.clear()
        for i in ids.values():
            STORE[i] = {"seed": True}
        STORE["99999"] = {"seed": True}
        conn.execute("UPDATE files SET t3_embedded = 1")
        conn.commit()

    def flags():
        return {r["name"]: r["t3_embedded"] for r in conn.execute(
            "SELECT name, t3_embedded FROM files")}

    def main(*argv):
        sys.argv = ["bytecat embed", *argv]
        out, err = io.StringIO(), io.StringIO()
        code = None
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = embed.main()
            except SystemExit as e:
                code = e.code
        return code, out.getvalue() + err.getvalue()

    # 1. Model server down: nothing is deleted.
    seed()
    FakeLM.up = False
    code, out = main("--reset")
    check("reset with no model server exits 1", code == 1, f"{code} {out}")
    check("reset with no model server deletes no vectors and clears no flags",
          len(STORE) == 4 and set(flags().values()) == {1}, f"{sorted(STORE)} {flags()}")
    FakeLM.up = True

    # 2. --limit with --reset is refused before anything runs.
    seed()
    code, out = main("--reset", "--limit", "1")
    check("--reset with --limit is refused",
          code == 2 and len(STORE) == 4 and set(flags().values()) == {1}, f"{code} {out}")

    # 3. A reset rebuilds this host's files and leaves another host's alone.
    seed()
    code, out = main("--reset")
    check("reset exits 0", code == 0, out)
    check("reset keeps another host's flags", flags()["c.txt"] == 1, str(flags()))
    check("and its vector", STORE.get(ids["c.txt"]) == {"seed": True}, str(STORE))
    check("this host's files are re-embedded",
          all(flags()[n] == 1 and "seed" not in STORE.get(ids[n], {"seed": 1})
              for n in ("a.txt", "b.jpg")), f"{flags()} {STORE}")
    check("a vector with no catalog row is deleted", "99999" not in STORE, str(sorted(STORE)))

    # 4. --kinds narrows the reset to those kinds.
    seed()
    code, out = main("--reset", "--kinds", "document")
    check("--reset --kinds document keeps the image's vector",
          code == 0 and STORE.get(ids["b.jpg"]) == {"seed": True}
          and "seed" not in STORE.get(ids["a.txt"], {"seed": 1}), f"{code} {STORE}")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
