#!/usr/bin/env python3
"""C editor backend. stdlib only. Run: python3 server.py -> http://localhost:8000"""
import http.server
import json
import os
import re
import shutil
import subprocess
import tempfile

PORT = int(os.environ.get("PORT", 8000))
HOST = os.environ.get("HOST", "0.0.0.0")  # 0.0.0.0 for containers; see security note in DEPLOY.md
HERE = os.path.dirname(os.path.abspath(__file__))
ERR_RE = re.compile(r'^[^:\n]*prog\.c:(\d+):(\d+): (fatal error|error|warning|note): (.*)$', re.M)
RUN_TIMEOUT = 5
TRACE_TIMEOUT = 30
# clang-format: bare binary on Linux, xcrun wrapper on macOS
CLANG_FORMAT = ["clang-format"] if shutil.which("clang-format") else ["xcrun", "clang-format"]


def compile_c(code, extra=()):
    """Returns (tmpdir, binpath_or_None, diagnostics)."""
    d = tempfile.mkdtemp(prefix="cedit-")
    src = os.path.join(d, "prog.c")
    with open(src, "w") as f:
        f.write(code)
    binp = os.path.join(d, "prog")
    p = subprocess.run(
        ["gcc", "-g", "-O0", "-Wall", "-fdiagnostics-color=never", *extra, src, "-o", binp],
        capture_output=True, text=True, timeout=30)
    diags = [{"line": int(m[0]), "col": int(m[1]), "sev": m[2], "msg": m[3]}
             for m in ERR_RE.findall(p.stderr)]
    return d, (binp if p.returncode == 0 else None), diags


def api_compile(body):
    d, binp, diags = compile_c(body.get("code", ""))
    shutil.rmtree(d, ignore_errors=True)
    return {"ok": binp is not None, "diags": diags}


def api_run(body):
    d, binp, diags = compile_c(body.get("code", ""))
    try:
        if binp is None:
            return {"ok": False, "diags": diags, "stdout": "", "stderr": "", "exit": None}
        try:
            p = subprocess.run([binp], input=body.get("stdin", ""),
                               capture_output=True, text=True, timeout=RUN_TIMEOUT)
            return {"ok": True, "diags": diags, "stdout": p.stdout,
                    "stderr": p.stderr, "exit": p.returncode}
        except subprocess.TimeoutExpired as e:
            return {"ok": True, "diags": diags,
                    "stdout": (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""),
                    "stderr": "", "exit": None,
                    "timeout": RUN_TIMEOUT}
    finally:
        shutil.rmtree(d, ignore_errors=True)


def api_format(body):
    p = subprocess.run(
        [*CLANG_FORMAT, "-style={BasedOnStyle: LLVM, IndentWidth: 4, AllowShortFunctionsOnASingleLine: None}",
         "-assume-filename=prog.c"],
        input=body.get("code", ""), capture_output=True, text=True, timeout=15)
    if p.returncode != 0:
        return {"ok": False, "error": p.stderr}
    return {"ok": True, "code": p.stdout}


# unbuffered stdout so the visualizer shows printf output at the step it happens
PRELUDE = ("#include <stdio.h>\n"
           "__attribute__((constructor)) static void __cedit_unbuf(void)"
           "{ setvbuf(stdout, 0, _IONBF, 0); }\n")


def api_trace(body):
    pre = os.path.join(tempfile.gettempdir(), "cedit_prelude.h")
    with open(pre, "w") as f:
        f.write(PRELUDE)
    d, binp, diags = compile_c(body.get("code", ""), extra=("-include", pre))
    try:
        if binp is None:
            return {"ok": False, "diags": diags}
        stdin_file = os.path.join(d, "stdin.txt")
        with open(stdin_file, "w") as f:
            f.write(body.get("stdin", ""))
        out_file = os.path.join(d, "trace.json")
        env = dict(os.environ,
                   TRACE_BIN=binp, TRACE_SRC="prog.c",
                   TRACE_OUT=out_file, TRACE_STDIN=stdin_file)
        p = subprocess.run(
            ["lldb", "--batch", "--no-lldbinit",
             "-o", "command script import " + os.path.join(HERE, "trace_lldb.py")],
            env=env, capture_output=True, text=True, timeout=TRACE_TIMEOUT)
        if not os.path.exists(out_file):
            return {"ok": False, "diags": diags,
                    "error": "lldb trace failed:\n" + p.stdout[-2000:] + p.stderr[-2000:]}
        with open(out_file) as f:
            trace = json.load(f)
        trace.update(ok=True, diags=diags)
        return trace
    except subprocess.TimeoutExpired:
        return {"ok": False, "diags": diags, "error": "trace timed out (infinite loop?)"}
    finally:
        shutil.rmtree(d, ignore_errors=True)


ROUTES = {"/api/compile": api_compile, "/api/run": api_run,
          "/api/format": api_format, "/api/trace": api_trace}


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=HERE, **kw)

    def do_POST(self):
        fn = ROUTES.get(self.path)
        if not fn:
            self.send_error(404)
            return
        n = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
            resp = fn(body)
        except Exception as e:  # surface any backend failure to the UI
            resp = {"ok": False, "error": str(e)}
        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass  # ponytail: quiet server


if __name__ == "__main__":
    print(f"C editor -> http://localhost:{PORT}  (binding {HOST}:{PORT})")
    http.server.ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
