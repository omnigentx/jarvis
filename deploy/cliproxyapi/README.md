# CLIProxyAPI Deployment

This directory keeps `CLIProxyAPI` separate from the Jarvis application stack.

## Files

- `docker-compose.yml` runs `CLIProxyAPI` as its own Docker service on the Ubuntu host
- `config.example.yaml` is a starter config
- `login-codex.sh` runs a one-shot Codex OAuth login using SSH tunnel callback port `1455`

## First-time setup

1. Copy the sample config:

```bash
cd deploy/cliproxyapi
cp config.example.yaml config.yaml
mkdir -p auths logs
```

2. Edit `config.yaml`:

- keep `port: 8317`
- set `remote-management.secret-key`
- keep `api-keys` containing the same bearer Jarvis uses: `jarvis-proxy-key`

3. Start the service:

```bash
docker compose up -d
```

The service will listen on host port `8317`, which Jarvis reaches through
`http://host.docker.internal:8317/v1`.

## Codex OAuth login on a headless Ubuntu server

Open an SSH tunnel from your laptop:

```bash
ssh -L 1455:127.0.0.1:1455 <user>@<ubuntu-server>
```

Then on the server run:

```bash
cd deploy/cliproxyapi
./login-codex.sh
```

The script prints a login URL. Open that URL in your local browser.

Because the SSH tunnel forwards local port `1455` to the Ubuntu server, the
OAuth callback completes without needing a GUI or a public callback endpoint on
the server.

## Management API

By default this setup keeps remote management disabled.

If you need the management API, access it from the host:

```bash
curl -H 'Authorization: Bearer <MANAGEMENT_KEY>' \
  http://127.0.0.1:8317/v0/management/auth-files
```

## Notes

- Keep `CLIProxyAPI` separate from Jarvis deploys. Jarvis CD should not rebuild or redeploy the proxy.
- Tokens are persisted under `deploy/cliproxyapi/auths/`.
- Logs are persisted under `deploy/cliproxyapi/logs/`.
