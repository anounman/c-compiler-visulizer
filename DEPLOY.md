# Deploy

Single Docker image serves frontend + backend (gcc, lldb, clang-format).
**Not Vercel** — serverless can't run a compiler or a debugger (needs ptrace).

## Build & run

```sh
docker build -t cedit .
docker run -d -p 8000:8000 \
  --cap-add=SYS_PTRACE --security-opt seccomp=unconfined \
  cedit
```

Open http://localhost:8000

`--cap-add=SYS_PTRACE` + `--security-opt seccomp=unconfined` are **required** — lldb
attaches to the traced program with ptrace, which Docker's default sandbox blocks.

## ⚠️ Security — do not expose this publicly as-is

It compiles and **runs arbitrary C** submitted by the browser. Anyone who can reach
the port can run code in the container. The container is the only sandbox; there is
no auth, no per-user isolation, no resource cap beyond the 5s run / 30s trace timeout.

Safe uses: localhost, a classroom LAN, or behind auth (reverse proxy with a password /
VPN / SSO). Do **not** put it on the open internet.

## Hosting (any Docker host with ptrace)

- **Fly.io** — `fly launch` (uses this Dockerfile), Firecracker VMs allow ptrace natively.
  Put it behind Fly's auth or an `[http_service]` with access control.
- **Railway / Render** — deploy the Dockerfile. Confirm the platform grants SYS_PTRACE;
  some managed runners block it. If trace fails but run works, ptrace is blocked.
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
