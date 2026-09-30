# Optional MCP plugin sandbox

Skills work without Docker. Executable MCP contributions remain unavailable until
an operator supplies a reviewed, immutable image and a working container runtime.
Jarvis never falls back to host execution.

## Native local acceptance

Build this first-party image from the repository root:

```sh
docker build --build-arg BASE_IMAGE=python@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b \
  -t jarvis-plugin-sandbox:review build/plugin-sandbox
docker image inspect --format '{{.Id}}' jarvis-plugin-sandbox:review
```

Set `JARVIS_PLUGIN_SANDBOX_IMAGE` to the resulting `sha256:…` image ID before
starting the backend. Settings → Plugins reports whether the runtime can inspect
that locally available image. Configure and review each package's execution
policy before activation. The shipped image contains Python and the hash-locked
MCP SDK. Plugins needing Node or other dependencies require a separately reviewed
image containing those dependencies; runtime package installation is not enabled.

## Backend deployed inside Docker

The standard backend image does **not** include a Docker client or container daemon
access. MCP execution is therefore opt-in, not enabled by the existing Compose
configuration. Operators must provide a backend image with the client, a suitably
protected daemon connection, and consistent package mount paths. Daemon access is
a privileged host capability: do not give plugins its socket or connection details.
No such deployment change is made by this PR.

When backend and daemon see different paths, set `JARVIS_PLUGIN_RUNTIME_ROOT`
(the backend's plugin storage root) and `JARVIS_PLUGIN_HOST_ROOT` (the same storage
as seen by the daemon). The path mapping rejects packages outside the configured
root. Verify the operator configuration with the real OCI isolation tests before
making execution available to users.

## Enforced execution policy

Each process runs as UID 65532, with network disabled, read-only root/package,
no host credentials or daemon socket, dropped capabilities, no-new-privileges,
bounded memory/CPU/PIDs, and a small noexec temporary filesystem. Images are pinned
and never pulled during activation. Explicit credential slots are encrypted in
SQLite and bound to the human-reviewed policy revision; secret values are not
published in inventory or approvals. This networkless profile cannot run remote
API integrations; those contributions fail closed pending a reviewed transport.

Runtime download avoids bundling third-party plugins in Jarvis. It does not replace
review of upstream license terms. The UI links to the pinned source and exposes
package files as text before activation.
