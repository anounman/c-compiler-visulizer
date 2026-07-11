# C Compiler Visualizer

A small, self-contained C playground for learning how programs execute. Write C in the browser, compile and run it, or replay an LLDB trace while watching stack frames, variables, arrays, structs, pointers, and heap allocations change line by line.

The frontend is a single HTML file and the backend uses only Python's standard library. GCC, LLDB, and clang-format provide the compiler, debugger, and formatter.

## Features

- Live GCC diagnostics with errors and warnings shown beside the source
- Program execution with custom `stdin`, captured output, exit codes, and a 5-second timeout
- Source formatting through clang-format
- Line-by-line execution playback with play, pause, step, scrub, and breakpoint controls
- Stack-frame and in-scope local-variable inspection
- Visualizations for arrays, matrices, structs, pointers, linked structures, and heap memory
- Pointer arrows, changed-value highlighting, and crash reporting
- Source persisted locally in the browser
- No frontend build step or third-party Python packages

## Quick start with Docker

Docker is the most reproducible way to run the complete application.

```sh
git clone https://github.com/anounman/c-compiler-visulizer.git
cd c-compiler-visulizer
docker build -t c-compiler-visualizer .
docker run --rm -p 8000:8000 \
  --cap-add=SYS_PTRACE \
  --security-opt seccomp=unconfined \
  c-compiler-visualizer
```

Open [http://localhost:8000](http://localhost:8000).

LLDB needs `ptrace` access to trace the compiled program, so both Docker security options are required. Compile and run may still work without them, but visualization will fail.

## Run locally

Install the following tools and make sure they are available on `PATH`:

- Python 3
- GCC
- LLDB with Python scripting support
- clang-format

On Ubuntu/Debian:

```sh
sudo apt update
sudo apt install gcc libc6-dev lldb python3 clang-format
python3 server.py
```

On macOS, install the Xcode Command Line Tools and clang-format:

```sh
xcode-select --install
brew install clang-format
python3 server.py
```

Then open [http://localhost:8000](http://localhost:8000). You can change the bind address and port with environment variables:

```sh
HOST=127.0.0.1 PORT=3000 python3 server.py
```

| Variable | Default | Purpose |
| --- | --- | --- |
| `HOST` | `0.0.0.0` | Address used by the HTTP server |
| `PORT` | `8000` | HTTP server port |
| `APP_VERSION` | `dev` | Current semantic release tag, embedded by the Docker build |

## Publishing a Docker Hub release

Published images use semantic version tags. Build each release with its version embedded,
then push both the immutable version tag and the convenient `latest` tag:

```sh
VERSION=v1.0.0
docker build --build-arg APP_VERSION="$VERSION" \
  -t anounman/c-editor:"$VERSION" \
  -t anounman/c-editor:latest .
docker push anounman/c-editor:"$VERSION"
docker push anounman/c-editor:latest
```

When a newer stable `vX.Y.Z` tag appears on Docker Hub, older running versions show a
dismissible update notification in the editor. Open browser sessions check every 15 minutes.
Docker Hub lookup failures are ignored and retried after the server's cache expires.

## Using the visualizer

1. Enter a C program in the editor.
2. Add any input expected by `scanf` to the **stdin** box.
3. Select **Run** to compile and execute the program.
4. Select **Visualize** to record a trace and open the memory view.
5. Step with the arrow buttons or keyboard arrow keys, press **Play**, or drag the timeline.
6. Click a source line number to toggle a breakpoint. The breakpoint button advances to the next selected line.

Keyboard shortcuts:

| Shortcut | Action |
| --- | --- |
| `Ctrl`/`Cmd` + `Enter` | Compile and run |
| `Shift` + `Alt` + `F` | Format source |
| `Left Arrow` / `Right Arrow` | Move through a trace while the memory view is active |

## How it works

```text
Browser (index.html)
        |
        | JSON over HTTP
        v
Python server (server.py)
   |         |          |
   v         v          v
  GCC   clang-format   LLDB + trace_lldb.py
   |                    |
 program output         v
                  execution trace JSON
```

Each request gets a temporary source file. Compile and run requests invoke GCC directly. Visualization compiles with debug symbols and asks LLDB to step through user source, collecting call frames, local values, addresses, pointed-to data, output, and crashes. The browser replays that trace without keeping a debugger process open.

### HTTP API

All endpoints accept JSON via `POST`.

| Endpoint | Request fields | Result |
| --- | --- | --- |
| `/api/compile` | `code` | Compile status and diagnostics |
| `/api/run` | `code`, `stdin` | Diagnostics, stdout, stderr, and exit status |
| `/api/format` | `code` | Formatted C source |
| `/api/trace` | `code`, `stdin` | Execution steps, frames, memory state, output, and crash details |

## Project structure

| File | Description |
| --- | --- |
| `index.html` | Editor, output panel, trace playback, and memory visualization |
| `server.py` | Static HTTP server and compile, run, format, and trace endpoints |
| `trace_lldb.py` | LLDB Python script that records execution state as JSON |
| `Dockerfile` | Ubuntu image containing the full runtime toolchain |
| `DEPLOY.md` | Container deployment notes and security guidance |

## Limits and security

> [!WARNING]
> This application compiles and executes arbitrary C submitted by the browser. It has no authentication or per-user isolation. Do not expose it directly to the public internet.

Use it locally, on a trusted classroom network, or behind authentication and network controls. The Docker container is only a coarse isolation boundary; a production multi-user service needs stronger sandboxing, quotas, and per-execution isolation. See [DEPLOY.md](DEPLOY.md) for deployment guidance.

Current safeguards and visualization limits include:

- Program runs are terminated after 5 seconds.
- Trace requests are terminated after 30 seconds.
- A trace records at most 400 debugger steps.
- Aggregate displays are capped at 24 children per level.
- Pointer traversal is capped at 12 levels.

These bounds keep the teaching UI responsive; they are not a security sandbox.

## Troubleshooting

**Visualization fails in Docker**

Run the container with `--cap-add=SYS_PTRACE --security-opt seccomp=unconfined`. Some managed container platforms prohibit debugger access even when regular execution works.

**The UI says `server offline?`**

Start the backend with `python3 server.py` and load the page through the server URL. Opening `index.html` directly does not provide the API endpoints.

**Formatting fails**

Confirm that `clang-format` is installed. On macOS the backend also attempts to use `xcrun clang-format` when a standalone binary is not present.

**Compilation fails without useful diagnostics**

Confirm that the `gcc` command is installed and callable from the same shell used to launch the server.
