#!/usr/bin/env python3
"""C editor backend. stdlib only. Run: python3 server.py -> http://localhost:8000"""
import base64
import http.server
import json
import os
import pty
import select
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import re
import uuid
from urllib.parse import urlparse, parse_qs

PORT = int(os.environ.get("PORT", 8000))
HOST = os.environ.get("HOST", "0.0.0.0")  # 0.0.0.0 for containers; see security note in DEPLOY.md
HERE = os.path.dirname(os.path.abspath(__file__))
ERR_RE = re.compile(r'^[^:\n]*prog\.c:(\d+):(\d+): (fatal error|error|warning|note): (.*)$', re.M)
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


# unbuffered stdout so prompts (printf without \n) appear before we block on input
PRELUDE = ("#include <stdio.h>\n"
           "__attribute__((constructor)) static void __cedit_unbuf(void)"
           "{ setvbuf(stdout, 0, _IONBF, 0); }\n")
PRELUDE_FILE = os.path.join(tempfile.gettempdir(), "cedit_prelude.h")
with open(PRELUDE_FILE, "w") as _f:
    _f.write(PRELUDE)

# --- interactive run: program runs under a pty, browser streams I/O live ---
SESSIONS = {}                    # sid -> {"pid", "fd", "start", "dir"}
SESSIONS_LOCK = threading.Lock()
RUN_CAP = 120                    # ponytail: hard wall-clock cap; a run can't outlive this


def _reap(sess):
    """Kill (if alive) and clean up a session. Returns exit code or None."""
    code = None
    try:
        pid, status = os.waitpid(sess["pid"], os.WNOHANG)
        if pid == 0:                      # still alive -> kill it
            os.kill(sess["pid"], signal.SIGKILL)
            os.waitpid(sess["pid"], 0)
        elif os.WIFEXITED(status):
            code = os.WEXITSTATUS(status)
        elif os.WIFSIGNALED(status):
            code = -os.WTERMSIG(status)
    except (OSError, ChildProcessError):
        pass
    try:
        os.close(sess["fd"])
    except OSError:
        pass
    shutil.rmtree(sess["dir"], ignore_errors=True)
    return code


def api_start(body):
    d, binp, diags = compile_c(body.get("code", ""), extra=("-include", PRELUDE_FILE))
    if binp is None:
        shutil.rmtree(d, ignore_errors=True)
        return {"ok": False, "diags": diags}
    pid, fd = pty.fork()
    if pid == 0:                          # child: become the student's program
        try:
            os.chdir(d)
            os.execv(binp, [binp])
        except Exception:
            os._exit(127)
    sid = uuid.uuid4().hex
    with SESSIONS_LOCK:
        SESSIONS[sid] = {"pid": pid, "fd": fd, "start": time.time(), "dir": d}
    return {"ok": True, "sid": sid, "diags": diags}


def api_input(body):
    sess = SESSIONS.get(body.get("sid"))
    if not sess:
        return {"ok": False}
    try:
        os.write(sess["fd"], body.get("data", "").encode())
        return {"ok": True}
    except OSError:
        return {"ok": False}


def api_stop(body):
    with SESSIONS_LOCK:
        sess = SESSIONS.pop(body.get("sid"), None)
    if sess:
        _reap(sess)
    return {"ok": True}


def api_format(body):
    p = subprocess.run(
        [*CLANG_FORMAT, "-style={BasedOnStyle: LLVM, IndentWidth: 4, AllowShortFunctionsOnASingleLine: None}",
         "-assume-filename=prog.c"],
        input=body.get("code", ""), capture_output=True, text=True, timeout=15)
    if p.returncode != 0:
        return {"ok": False, "error": p.stderr}
    return {"ok": True, "code": p.stdout}


def api_trace(body):
    d, binp, diags = compile_c(body.get("code", ""), extra=("-include", PRELUDE_FILE))
    try:
        if binp is None:
            return {"ok": False, "diags": diags}
        stdin_file = os.path.join(d, "stdin.txt")
        with open(stdin_file, "w") as f:
            f.write(body.get("stdin", ""))
        out_file = os.path.join(d, "trace.json")
        reads_stdin = re.search(r'\b(scanf|fgets|getchar|getline|gets|getc|fgetc)\s*\(',
                                body.get("code", ""))
        env = dict(os.environ,
                   TRACE_BIN=binp, TRACE_SRC="prog.c",
                   TRACE_OUT=out_file, TRACE_STDIN=stdin_file,
                   TRACE_DETECT_EOF="1" if reads_stdin else "0")
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


ROUTES = {"/api/compile": api_compile, "/api/start": api_start,
          "/api/input": api_input, "/api/stop": api_stop,
          "/api/format": api_format, "/api/trace": api_trace}


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=HERE, **kw)

    def do_GET(self):
        if urlparse(self.path).path == "/api/stream":
            return self.stream()
        return super().do_GET()

    def stream(self):
        """SSE: push the program's live output; end with an `exit` event."""
        sid = parse_qs(urlparse(self.path).query).get("sid", [""])[0]
        sess = SESSIONS.get(sid)
        if not sess:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        fd = sess["fd"]
        code = None
        try:
            while True:
                if time.time() - sess["start"] > RUN_CAP:
                    break
                r, _, _ = select.select([fd], [], [], 0.5)
                if r:
                    try:
                        data = os.read(fd, 4096)
                    except OSError:        # pty EIO on Linux == child exited
                        break
                    if not data:
                        break
                    b64 = base64.b64encode(data).decode()
                    self.wfile.write(f"data: {b64}\n\n".encode())
                    self.wfile.flush()
                else:
                    # no output — child may have exited without closing pty yet
                    pid, status = os.waitpid(sess["pid"], os.WNOHANG)
                    if pid != 0:
                        code = (os.WEXITSTATUS(status) if os.WIFEXITED(status)
                                else -os.WTERMSIG(status) if os.WIFSIGNALED(status) else None)
                        # drain any last bytes
                        try:
                            data = os.read(fd, 65536)
                            if data:
                                self.wfile.write(f"data: {base64.b64encode(data).decode()}\n\n".encode())
                        except OSError:
                            pass
                        break
        except (BrokenPipeError, ConnectionResetError):
            pass  # browser closed the tab; fall through to cleanup
        with SESSIONS_LOCK:
            SESSIONS.pop(sid, None)
        reaped = _reap(sess)          # always: kill-if-alive, close fd, rm tmpdir
        if code is None:
            code = reaped
        try:
            self.wfile.write(f"event: exit\ndata: {code}\n\n".encode())
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

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
