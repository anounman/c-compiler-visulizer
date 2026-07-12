# Deploy

Single Docker image serves frontend + backend (gcc, lldb, clang-format).

## Vercel protected preview

Vercel supports container-backed Functions and detects `Dockerfile.vercel` in the
repository root. That image contains the complete GCC, LLDB, clang-format, and Python
runtime and listens on Vercel's default container port (`80`).

Before deploying, enable Deployment Protection in the Vercel project. This application
executes arbitrary C and must not be exposed as an unrestricted public service.

Deploy from the repository root:

```sh
vercel login
vercel link
vercel
```

After the preview passes the checks below, create the production deployment:

```sh
vercel --prod
```

Vercel can also build automatically after importing the GitHub repository. Leave the
framework preset on automatic; `Dockerfile.vercel` is detected without a build command
or output-directory setting.

### Preview verification

Replace the hostname with the URL printed by Vercel:

For a protected deployment, authenticate these requests with a Vercel protection
bypass secret or run the equivalent checks from an authenticated browser session.

```sh
DEPLOYMENT_URL=https://your-preview.vercel.app
curl --fail "$DEPLOYMENT_URL/api/health"
curl --fail -H 'Content-Type: application/json' \
  --data '{"code":"int main(void) { return 0; }"}' \
  "$DEPLOYMENT_URL/api/compile"
curl --fail -H 'Content-Type: application/json' \
  --data '{"code":"int main(void) { int x = 1; return x - 1; }"}' \
  "$DEPLOYMENT_URL/api/trace"
```

The deployment is ready only when health reports all three tools, compile returns
`"ok": true`, and trace returns at least one step. LLDB tracing depends on Vercel's
container runtime allowing a process to trace its own child, so this must be verified
on the actual preview before promoting it.

Submitted programs receive a minimal environment and, when the container starts as
root, run as the unprivileged `nobody` user. The server also re-executes itself with an
allowlisted environment before accepting traffic, preventing submitted programs from
recovering Vercel credentials through their own environment or `/proc/1/environ`. This
reduces credential exposure but is not a complete sandbox.

### Vercel runtime caveat

Compile, format, visualization, and Run are self-contained requests on Vercel. Run uses a
five-second inline fallback because an in-memory process and a later event-stream request
can land on different autoscaled instances. This means Vercel Run is non-interactive;
Docker/local deployments retain the live PTY terminal. A public multi-user release should
move each execution into Vercel Sandbox and persist the sandbox/session ID outside the
Function process.

## Build & run

```sh
docker compose up --build -d
```

Open http://localhost:8000

The Compose service always uses `127.0.0.1:8000`, chooses its container name, and
uses Docker's default security profile. LLDB keeps ASLR enabled and traces only the
program it launches, so `SYS_PTRACE` and `seccomp=unconfined` are not required on a
standard Docker installation.

For the published image without Compose:

```sh
docker run --rm -p 127.0.0.1:8000:8000 anounman/c-editor:latest
```

## ⚠️ Security — do not expose this publicly as-is

It compiles and **runs arbitrary C** submitted by the browser. Anyone who can reach
the port can run code in the container. The container is the only sandbox; there is
no auth, no per-user isolation, no resource cap beyond the 5s run / 30s trace timeout.

Safe uses: localhost, a classroom LAN, or behind auth (reverse proxy with a password /
VPN / SSO). Do **not** put it on the open internet.

## Hosting

- **Vercel** — deploys `Dockerfile.vercel` as a container-backed Function. Use a
  protected preview for validation; use Vercel Sandbox before public multi-user access.
- **Fly.io** — `fly launch` (uses this Dockerfile), Firecracker VMs allow child tracing.
  Put it behind Fly's auth or an `[http_service]` with access control.
- **Railway / Render** — deploy the Dockerfile. Some hardened managed runners block all
  debugging calls. If trace fails but run works, child tracing is blocked.
- **A plain VM** (Hetzner, EC2, a spare box) — the `docker run` above, firewalled to
  your users.

`PORT` env is honored (defaults 8000); `HOST` defaults `0.0.0.0`.

## Versioned Docker Hub releases

Embed the semantic release version while building, and publish the same image under its
version and `latest`:

```sh
VERSION=v1.0.0
docker build --build-arg APP_VERSION="$VERSION" \
  -t anounman/c-editor:"$VERSION" -t anounman/c-editor:latest .
docker push anounman/c-editor:"$VERSION"
docker push anounman/c-editor:latest
```

The app checks the public Docker Hub tags endpoint at most once every 15 minutes. A running
older semantic version shows users a notification; development builds (`APP_VERSION=dev`)
do not check Docker Hub.
