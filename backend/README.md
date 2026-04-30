# Jarvis Backend

## Running the Server

To start the backend server, run the following command from the `backend` directory:

```bash
uv run uvicorn server:app --host 0.0.0.0 --port 8000
```

This starts the FastAPI application defined in `server.py`, which initializes the FastAgent framework.

## Docker deployment — pre-flight step

Before the first `docker compose up` on a fresh host, create the git
credential bind-mount targets so Docker binds files (not empty dirs):

```bash
./scripts/ensure_git_credential_files.sh
docker compose up -d
```

`make build` / `make deploy` run the script automatically via the
`prep-git-credentials` target. **Skipping this step on a raw `docker compose up`
will make Docker create empty directories at `backend/git-credentials` and
`backend/gitconfig`**, which then block the sync service from writing files and
require `rm -rf` on both paths to recover. The script is idempotent — safe to
re-run whenever.
