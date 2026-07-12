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
import sys
import tempfile
import threading
import time
import re
import uuid
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen

PORT = int(os.environ.get("PORT", 8000))
HOST = os.environ.get("HOST", "0.0.0.0")  # 0.0.0.0 for containers; see security note in DEPLOY.md
HERE = os.path.dirname(os.path.abspath(__file__))
ERR_RE = re.compile(r'^[^:\n]*?([A-Za-z0-9._-]+\.[ch]):(\d+):(\d+): (fatal error|error|warning|note): (.*)$', re.M)
FNAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')  # rejects '/', '..', leading dot
TRACE_TIMEOUT = 30
APP_VERSION = os.environ.get("APP_VERSION", "dev")
DOCKER_IMAGE = "anounman/c-editor"
DOCKER_TAGS_URL = f"https://hub.docker.com/v2/repositories/{DOCKER_IMAGE}/tags?page_size=100"
DOCKER_PAGE_URL = f"https://hub.docker.com/r/{DOCKER_IMAGE}/tags"
SEMVER_RE = re.compile(r'^v?(\d+)\.(\d+)\.(\d+)$')
UPDATE_CACHE_TTL = 15 * 60
UPDATE_CACHE = {"checked_at": 0.0, "value": None}
UPDATE_CACHE_LOCK = threading.Lock()
# clang-format: bare binary on Linux, xcrun wrapper on macOS
CLANG_FORMAT = ["clang-format"] if shutil.which("clang-format") else ["xcrun", "clang-format"]
SERVER_ENV_ALLOWLIST = {
    "PATH", "LANG", "LC_ALL", "TZ", "HOME", "HOST", "PORT", "APP_VERSION",
    "VERCEL", "VERCEL_ENV", "VERCEL_URL", "VERCEL_REGION", "VERCEL_TARGET_ENV",
    "VERCEL_GIT_COMMIT_SHA",
}


def sanitized_server_env():
    """Environment safe to expose through the compiler container's PID 1."""
    return {key: value for key, value in os.environ.items()
            if key in SERVER_ENV_ALLOWLIST}


def maybe_reexec_with_sanitized_env():
    """Replace the container process so /proc/1/environ contains no secrets."""
    if (os.environ.get("CEDIT_SANITIZE_ENV") != "1" or
            os.environ.get("CEDIT_ENV_CLEAN") == "1"):
        return
    env = sanitized_server_env()
    env["CEDIT_ENV_CLEAN"] = "1"
    os.execve(sys.executable,
              [sys.executable, os.path.abspath(__file__), *sys.argv[1:]], env)


def api_health():
    """Runtime readiness used by Docker, Vercel, and deployment smoke tests."""
    tools = {name: shutil.which(name) is not None
             for name in ("gcc", "lldb", "clang-format")}
    return {"ok": all(tools.values()), "version": APP_VERSION, "tools": tools}


def semver(version):
    """Return a comparable stable semantic version tuple, or None."""
    match = SEMVER_RE.fullmatch(str(version).strip())
    return tuple(map(int, match.groups())) if match else None


def fetch_update_status(current=APP_VERSION, opener=urlopen):
    """Compare this image version with the newest stable Docker Hub tag."""
    current_tuple = semver(current)
    result = {"ok": True, "current": current, "update_available": False}
    if current_tuple is None:  # local/dev builds have no meaningful release version
        return result

    request = Request(DOCKER_TAGS_URL, headers={"User-Agent": "c-editor-update-check/1"})
    with opener(request, timeout=4) as response:
        payload = json.load(response)
    versions = [(semver(tag.get("name")), tag.get("name"))
                for tag in payload.get("results", []) if isinstance(tag, dict)]
    versions = [(version, name) for version, name in versions if version is not None]
    if not versions:
        return result

    latest_tuple, latest_name = max(versions, key=lambda item: item[0])
    result.update(latest=latest_name, update_available=latest_tuple > current_tuple,
                  url=DOCKER_PAGE_URL)
    return result


def api_update():
    """Cached, fail-quiet update status for the browser."""
    now = time.monotonic()
    with UPDATE_CACHE_LOCK:
        if (UPDATE_CACHE["value"] is not None and
                now - UPDATE_CACHE["checked_at"] < UPDATE_CACHE_TTL):
            return UPDATE_CACHE["value"]
        try:
            value = fetch_update_status()
        except Exception:
            value = {"ok": False, "current": APP_VERSION,
                     "update_available": False}
        UPDATE_CACHE.update(checked_at=now, value=value)
        return value


def clean_files(body):
    """{name: content} for the whole workspace; legacy single-file 'code' still works.
    Names are validated (FNAME_RE) so nothing can escape the tmpdir."""
    files = body.get("files") or {"prog.c": body.get("code", "")}
    out = {n: str(c) for n, c in files.items()
           if isinstance(n, str) and FNAME_RE.match(n)}
    out.setdefault("prog.c", "")
    return out


def compile_c(files, extra=()):
    """Returns (tmpdir, binpath_or_None, diagnostics). All *.c files compile together;
    headers and data files just land in the dir so fopen() finds them."""
    d = tempfile.mkdtemp(prefix="cedit-")
    for name, content in files.items():
        with open(os.path.join(d, name), "w") as f:
            f.write(content)
    srcs = [os.path.join(d, n) for n in sorted(files) if n.endswith(".c")]
    binp = os.path.join(d, "prog")
    p = subprocess.run(
        ["gcc", "-g", "-O0", "-Wall", "-fdiagnostics-color=never", *extra, *srcs, "-o", binp],
        capture_output=True, text=True, timeout=30)
    diags = [{"file": m[0], "line": int(m[1]), "col": int(m[2]), "sev": m[3], "msg": m[4]}
             for m in ERR_RE.findall(p.stderr)]
    return d, (binp if p.returncode == 0 else None), diags


SKIP_FILES = {"prog", "trace.json", "stdin.txt", "out.txt"}
MAX_FILE_OUT = 65536


def collect_new_files(d, submitted):
    """Files the program created or modified — sent back to the editor as tabs."""
    out = {}
    for n in sorted(os.listdir(d)):
        p = os.path.join(d, n)
        if n in SKIP_FILES or not os.path.isfile(p) or not FNAME_RE.match(n):
            continue
        if os.path.getsize(p) > MAX_FILE_OUT:
            continue  # ponytail: cap what we ship back; students' files are small
        with open(p, errors="replace") as f:
            content = f.read()
        if submitted.get(n) != content:
            out[n] = content
    return out


def api_compile(body):
    d, binp, diags = compile_c(clean_files(body))
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
UNPRIVILEGED_UID = 65534         # nobody/nogroup in the Ubuntu container
UNPRIVILEGED_GID = 65534


def program_env(workdir):
    """Small, non-secret environment inherited by submitted programs."""
    env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TZ")
           if os.environ.get(key)}
    env["HOME"] = workdir
    return env


def prepare_execution_dir(workdir):
    """Let the unprivileged program use its workspace when the server is root."""
    if os.geteuid() != 0:
        return None
    for root, dirs, files in os.walk(workdir):
        os.chown(root, UNPRIVILEGED_UID, UNPRIVILEGED_GID)
        for name in dirs + files:
            os.chown(os.path.join(root, name), UNPRIVILEGED_UID, UNPRIVILEGED_GID)
    return (UNPRIVILEGED_UID, UNPRIVILEGED_GID)


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
    files = clean_files(body)
    d, binp, diags = compile_c(files, extra=("-include", PRELUDE_FILE))
    if binp is None:
        shutil.rmtree(d, ignore_errors=True)
        return {"ok": False, "diags": diags}
    identity = prepare_execution_dir(d)
    pid, fd = pty.fork()
    if pid == 0:                          # child: become the student's program
        try:
            os.chdir(d)
            if identity:
                uid, gid = identity
                os.setgroups([])
                os.setgid(gid)
                os.setuid(uid)
            os.execve(binp, [binp], program_env(d))
        except Exception:
            os._exit(127)
    sid = uuid.uuid4().hex
    with SESSIONS_LOCK:
        SESSIONS[sid] = {"pid": pid, "fd": fd, "start": time.time(),
                         "dir": d, "files": files}
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
    files = clean_files(body)
    d, binp, diags = compile_c(files, extra=("-include", PRELUDE_FILE))
    try:
        if binp is None:
            return {"ok": False, "diags": diags}
        stdin_file = os.path.join(d, "stdin.txt")
        with open(stdin_file, "w") as f:
            f.write(body.get("stdin", ""))
        identity = prepare_execution_dir(d)
        out_file = os.path.join(d, "trace.json")
        reads_stdin = re.search(r'\b(scanf|fgets|getchar|getline|gets|getc|fgetc)\s*\(',
                                "\n".join(files.values()))
        env = dict(os.environ,
                   TRACE_BIN=binp, TRACE_SRC="prog.c",
                   TRACE_OUT=out_file, TRACE_STDIN=stdin_file,
                   TRACE_DETECT_EOF="1" if reads_stdin else "0",
                   TRACE_PROGRAM_ENV=json.dumps(program_env(d)),
                   TRACE_UID=str(identity[0]) if identity else "",
                   TRACE_GID=str(identity[1]) if identity else "")
        p = subprocess.run(
            ["lldb", "--batch", "--no-lldbinit",
             "-o", "command script import " + os.path.join(HERE, "trace_lldb.py")],
            env=env, capture_output=True, text=True, timeout=TRACE_TIMEOUT)
        if not os.path.exists(out_file):
            return {"ok": False, "diags": diags,
                    "error": "lldb trace failed:\n" + p.stdout[-2000:] + p.stderr[-2000:]}
        with open(out_file) as f:
            trace = json.load(f)
        if trace.get("error"):
            return {"ok": False, "diags": diags,
                    "error": "LLDB could not start the program:\n" + trace["error"]}
        if not trace.get("steps"):
            return {"ok": False, "diags": diags,
                    "error": "No executable lines in prog.c were reached. "
                             "Make sure prog.c contains the code called from main()."}
        trace.update(ok=True, diags=diags,
                     files_out=collect_new_files(d, files))
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
        path = urlparse(self.path).path
        if path == "/api/stream":
            return self.stream()
        if path == "/api/health":
            payload = api_health()
            data = json.dumps(payload).encode()
            self.send_response(200 if payload["ok"] else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/update":
            data = json.dumps(api_update()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
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
        try:  # grab program-written files BEFORE _reap deletes the dir
            new_files = collect_new_files(sess["dir"], sess.get("files", {}))
        except OSError:
            new_files = {}
        reaped = _reap(sess)          # always: kill-if-alive, close fd, rm tmpdir
        if code is None:
            code = reaped
        try:
            if new_files:  # json.dumps escapes newlines -> safe as one SSE data line
                self.wfile.write(("event: files\ndata: " + json.dumps(new_files) + "\n\n").encode())
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
    maybe_reexec_with_sanitized_env()
    print(f"C editor -> http://localhost:{PORT}  (binding {HOST}:{PORT})")
    http.server.ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
