# Self-Hosting Jarvis on Ubuntu Server

Guide to deploying Jarvis (Backend + Vue Web) on an Ubuntu server using Docker + GitHub Actions CD.

## Requirements

- Ubuntu 22.04+ (or 24.04)
- RAM: 2GB minimum (4GB recommended)
- Disk: 20GB+
- Domain pointed at the server (optional, for SSL)

---

## 1. Install Docker & Docker Compose

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Docker
curl -fsSL https://get.docker.com | sudo sh

# Add user to the docker group (no more sudo needed)
sudo usermod -aG docker $USER

# Logout & login again for the group to take effect
exit
# SSH back into the server

# Verify
docker --version
docker compose version
```

## 2. Install Git

```bash
sudo apt install -y git
```

## 3. Clone the repo (with submodules)

```bash
cd ~
git clone --recurse-submodules https://github.com/omnigentx/jarvis.git
cd ~/jarvis
```

> If you already cloned but the submodules are missing:
> ```bash
> git submodule update --init --recursive
> ```

## 4. Create secrets files

### `.env` (backend environment)

```bash
cat > backend/.env << 'EOF'
# Voice config (TTS engines, STT, wake-word) is DB-backed — manage via
# Settings → Voice in the dashboard. No env var needed for voice.

# --- JWT ---
JWT_SECRET=REPLACE_WITH_A_LONG_RANDOM_STRING
JWT_ALGORITHM=HS256
JWT_EXPIRE_DAYS=7

# --- CORS ---
CORS_ORIGINS=*

# --- Logging ---
LOG_CONSOLE_LEVEL=INFO
PYTHONUNBUFFERED=1
EOF
```

> ⚠️ **Important**: Replace `JWT_SECRET` with a long random string:
> ```bash
> openssl rand -hex 32
> ```

### `fastagent.secrets.yaml` (API keys for AI)

```bash
cat > backend/fastagent.secrets.yaml << 'EOF'
mcp:
  servers:
    github:
      env:
        GITHUB_PERSONAL_ACCESS_TOKEN: ""
    brave-search:
      env:
        BRAVE_API_KEY: ""

anthropic:
  api_key: "sk-ant-xxx"

openai:
  api_key: "sk-xxx"
  base_url: "https://api.openai.com/v1"

google:
  api_key: "xxx"
EOF
```

### Credentials (if any)

```bash
mkdir -p backend/config/credentials
# Copy credential files here:
# - firebase-adminsdk.json
# - google-cloud-tts.json
# - etc.
```

## 5. Open the Firewall

```bash
sudo ufw allow 22/tcp    # SSH
sudo ufw allow 80/tcp    # HTTP
sudo ufw allow 443/tcp   # HTTPS (if using SSL)
sudo ufw enable
```

## 6. Build & launch

```bash
cd ~/jarvis
docker compose build
docker compose up -d
```

Check:
```bash
# View logs
docker compose logs -f

# View status
docker compose ps

# Test API
curl http://localhost:8000/api/health
```

Open a browser: `http://<SERVER-IP>` → Jarvis Web UI.

---

## 7. Set up CD with a GitHub Actions Self-Hosted Runner

The runner lives on the server itself — every push to `main` makes GitHub Actions build the Docker images and deploy automatically.

### Step 1: Get a Runner Token

1. Go to the GitHub repo → **Settings** → **Actions** → **Runners**
2. Click **"New self-hosted runner"**
3. Select **Linux** → copy the token from the `./config.sh` command

### Step 2: Install the Runner

```bash
# Create the runner directory
mkdir -p ~/actions-runner && cd ~/actions-runner

# Download the runner (check the latest version: https://github.com/actions/runner/releases)
curl -o actions-runner-linux-x64.tar.gz -L \
  https://github.com/actions/runner/releases/download/v2.321.0/actions-runner-linux-x64-2.321.0.tar.gz
tar xzf actions-runner-linux-x64.tar.gz

# Configure
./config.sh --url https://github.com/omnigentx/jarvis --token YOUR_TOKEN_HERE

# Install as a system service (auto-start on reboot)
sudo ./svc.sh install
sudo ./svc.sh start

# Verify
sudo ./svc.sh status
```

### Step 3: Verify the runner is online

Go to the GitHub repo → **Settings** → **Actions** → **Runners** → the runner should show as **"Idle"** (online).

### How the CD flow works

```
Push to main → GitHub Actions triggers deploy.yml
  → Self-hosted runner checks out code (with submodules)
  → restores secrets from ~/jarvis-data into backend/
  → docker compose build (builds images on the server)
  → docker compose up -d --force-recreate (deploy)
  → Health check
```

### Important notes on Docker secrets

- The `backend/fastagent.secrets.docker.yaml` file in the runner workspace is only a temporary copy.
- The real source, restored by the workflow on every deploy, is `~/jarvis-data/fastagent.secrets.docker.yaml`.
- If prod returns `401 Invalid API key` when calling through CLIProxyAPI, check this persistent file first.
- With the current architecture, `openai.api_key` in the persistent file must be `jarvis-proxy-key`, not `sk-proj-...`.

---

## 8. (Optional) SSL with Nginx + Let's Encrypt

If you have a domain, set up SSL:

```bash
# Change the Docker web port to avoid conflicting with host nginx
# Edit docker-compose.yaml: ports "3080:80" instead of "80:80"

# Install Nginx + Certbot
sudo apt install -y nginx certbot python3-certbot-nginx

# Create the nginx config
sudo tee /etc/nginx/sites-available/jarvis << 'NGINX'
server {
    listen 80;
    server_name your-domain.com;

    client_max_body_size 16m;

    location / {
        proxy_pass http://127.0.0.1:3080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE support
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
    }
}
NGINX

# Enable site
sudo ln -sf /etc/nginx/sites-available/jarvis /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx

# SSL certificate
sudo certbot --nginx -d your-domain.com
```

> Replace `your-domain.com` with your real domain.

---

## 9. (Optional) Voice from outside your network — Cloudflare TURN

**When you need this:** only when you use voice chat from **outside** the network the server lives on — e.g. a phone on 4G/5G, office Wi-Fi. On `localhost`, the same LAN, or over a VPN (Tailscale, …) you do **not** need this section.

**Why:** voice uses WebRTC (UDP) flowing directly between the browser and the server — it does NOT ride the HTTP/tunnel path. If the server has no public UDP inbound (behind home NAT, behind a cloudflared tunnel) and the client sits behind carrier CGNAT, the two sides can never connect directly — a TURN relay must broker the traffic. This is the industry-standard fix (Meet/Zoom/Discord all run equivalent infrastructure).

**How to enable (≈3 minutes, free 1 TB/month):**

1. Open the [Cloudflare Dashboard → Realtime → TURN](https://dash.cloudflare.com/?to=/:account/calls) (a free Cloudflare account is enough)
2. Click **Create** and give the TURN app any name (e.g. `jarvis-voice`)
3. Copy the **Turn Token ID** and the **API Token**
4. Paste them into Jarvis under **Settings → Voice → Cloudflare TURN (voice relay)** — or enter them in the Services step of the Setup Wizard. Applies immediately, no restart.

The key is stored encrypted in the server's DB and never reaches the browser — browsers only receive short-lived (24 h) credentials minted from it.

> Headless/CI fallback: set the env vars `JARVIS_CF_TURN_KEY_ID` + `JARVIS_CF_TURN_API_TOKEN` (DB-stored values take priority when both exist), or point `JARVIS_WEBRTC_ICE` at a self-hosted TURN server — see `backend/.env.example`.

---

## Manual deploy (when needed)

```bash
cd ~/jarvis
git pull --recurse-submodules
docker compose build
docker compose up -d --force-recreate
```

---

## Updating the fast-agent submodule

```bash
cd ~/jarvis
git submodule update --remote backend/fast-agent
docker compose build jarvis-backend
docker compose up -d --force-recreate jarvis-backend
```

---

## Signing in with a Passkey (Touch ID / Face ID / Windows Hello)

After logging into the dashboard with `JARVIS_API_KEY`, you can register a passkey
so you never have to paste the key again — one Touch ID tap and you're in.

### First-time registration

1. Open the dashboard and sign in as usual with the `JARVIS_API_KEY` from
   `backend/.env`.
2. Go to **Settings → Authentication**.
3. Set a label (e.g. "MacBook Touch ID") and click **Register passkey**. The
   OS will show a prompt — touch the fingerprint sensor / camera to finish.
4. Sign out and reload the page. The **"Sign in with passkey"** button will appear.

### HTTPS requirement

WebAuthn (passkey) only works with:
- `http://localhost:*` (browsers make an exception for dev)
- `https://your-domain.com` (production)

A LAN IP like `http://192.168.1.50:3001` does **NOT** work with passkeys — the
browser will reject the ceremony. Set up HTTPS via Caddy / nginx + Let's Encrypt /
Tailscale Funnel before registering.

### Passkey scope is per-domain

A passkey is hard-bound to **RP ID = the hostname at registration time**. That means:
- A passkey registered on `localhost` → ONLY works when you access via
  `localhost`.
- Switching to `jarvis.alice.com` → you must register a new passkey for that domain.

This is a constraint of the WebAuthn spec, not a bug. When you move to a
production domain, open Settings → Authentication on the new domain and click
"Register passkey" again.

### Cross-device (laptop + phone)

Register a passkey on each device separately. iCloud Keychain / Google Password
Manager / 1Password will sync passkeys automatically if you use them; otherwise
register once per device.

### Recovery — lost passkeys

Passkeys cannot be exported. If you lose all your devices → the only way back is
to read `JARVIS_API_KEY` from `backend/.env` again, sign in with the API key, then
register a new passkey:

```bash
# On the server
grep JARVIS_API_KEY backend/.env
```

The API key is also the credential for scripts (Xiaozhi voice, CLI tools) — don't
delete it.

### Coexistence with the API key

Passkeys do not replace the API key. The two run in parallel:
- **Passkey**: browser login only.
- **API key in `.env`**: for Xiaozhi, scripts, recovery.

Settings → General has a rotate-API-key button if needed.

---

## Troubleshooting

| Problem | Fix |
|---------|----------|
| Container won't start | `docker compose logs jarvis-backend` |
| Port 80 already in use | `sudo lsof -i:80` → kill the process or change the port |
| Permission denied (Docker) | `sudo usermod -aG docker $USER` → logout/login again |
| Runner offline | `cd ~/actions-runner && sudo ./svc.sh status` |
| Runner not picking up jobs | Check the runner name matches the `self-hosted` label |
| Disk full | `docker system prune -a` to remove old images |
| Empty submodule | `git submodule update --init --recursive` |
| Backend crash loop | `docker compose logs -f jarvis-backend` → check .env and secrets |
| "Sign in with passkey" button missing | Register first from Settings → Authentication; HTTPS required except on localhost |
| Passkey not working on a LAN IP | Set up HTTPS (Caddy / Tailscale Funnel / mkcert) — WebAuthn only allows HTTPS + localhost |
| Lost all passkeys | Get `JARVIS_API_KEY` from `.env`, log in with the API key, register a new passkey |
