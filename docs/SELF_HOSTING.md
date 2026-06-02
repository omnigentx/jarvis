# Self-Hosting Jarvis trên Ubuntu Server

Hướng dẫn deploy Jarvis (Backend + Vue Web) trên Ubuntu server sử dụng Docker + GitHub Actions CD.

## Yêu cầu

- Ubuntu 22.04+ (hoặc 24.04)
- RAM: tối thiểu 2GB (khuyến nghị 4GB)
- Disk: 20GB+
- Domain trỏ về server (tùy chọn, cho SSL)

---

## 1. Cài Docker & Docker Compose

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Docker
curl -fsSL https://get.docker.com | sudo sh

# Add user vào docker group (không cần sudo nữa)
sudo usermod -aG docker $USER

# Logout & login lại để group có hiệu lực
exit
# SSH lại vào server

# Verify
docker --version
docker compose version
```

## 2. Cài Git

```bash
sudo apt install -y git
```

## 3. Clone repo (với submodule)

```bash
cd ~
git clone --recurse-submodules https://github.com/omnigentx/jarvis.git
cd ~/jarvis
```

> Nếu đã clone rồi nhưng thiếu submodule:
> ```bash
> git submodule update --init --recursive
> ```

## 4. Tạo file secrets

### `.env` (backend environment)

```bash
cat > backend/.env << 'EOF'
# Voice config (TTS engines, STT, wake-word) is DB-backed — manage via
# Settings → Voice in the dashboard. No env var needed for voice.

# --- JWT ---
JWT_SECRET=THAY_BANG_CHUOI_RANDOM_DAI
JWT_ALGORITHM=HS256
JWT_EXPIRE_DAYS=7

# --- CORS ---
CORS_ORIGINS=*

# --- Logging ---
LOG_CONSOLE_LEVEL=INFO
PYTHONUNBUFFERED=1
EOF
```

> ⚠️ **Quan trọng**: Thay `JWT_SECRET` bằng chuỗi random dài:
> ```bash
> openssl rand -hex 32
> ```

### `fastagent.secrets.yaml` (API keys cho AI)

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

### Credentials (nếu có)

```bash
mkdir -p backend/config/credentials
# Copy các file credentials vào đây:
# - firebase-adminsdk.json
# - google-cloud-tts.json
# - v.v.
```

## 5. Mở Firewall

```bash
sudo ufw allow 22/tcp    # SSH
sudo ufw allow 80/tcp    # HTTP
sudo ufw allow 443/tcp   # HTTPS (nếu dùng SSL)
sudo ufw enable
```

## 6. Build & khởi chạy

```bash
cd ~/jarvis
docker compose build
docker compose up -d
```

Kiểm tra:
```bash
# Xem logs
docker compose logs -f

# Xem status
docker compose ps

# Test API
curl http://localhost:8000/api/health
```

Mở browser: `http://<IP-SERVER>` → Jarvis Web UI.

---

## 7. Setup CD với GitHub Actions Self-Hosted Runner

Runner chạy trên chính server — mỗi khi push code lên `main`, GitHub Actions tự build Docker images và deploy.

### Bước 1: Lấy Runner Token

1. Vào GitHub repo → **Settings** → **Actions** → **Runners**
2. Click **"New self-hosted runner"**
3. Chọn **Linux** → Copy token từ lệnh `./config.sh`

### Bước 2: Cài Runner

```bash
# Tạo thư mục runner
mkdir -p ~/actions-runner && cd ~/actions-runner

# Download runner (check version mới nhất: https://github.com/actions/runner/releases)
curl -o actions-runner-linux-x64.tar.gz -L \
  https://github.com/actions/runner/releases/download/v2.321.0/actions-runner-linux-x64-2.321.0.tar.gz
tar xzf actions-runner-linux-x64.tar.gz

# Configure
./config.sh --url https://github.com/omnigentx/jarvis --token YOUR_TOKEN_HERE

# Cài như system service (tự start khi reboot)
sudo ./svc.sh install
sudo ./svc.sh start

# Verify
sudo ./svc.sh status
```

### Bước 3: Verify runner online

Vào GitHub repo → **Settings** → **Actions** → **Runners** → phải thấy runner ở trạng thái **"Idle"** (online).

### CD Flow hoạt động

```
Push to main → GitHub Actions triggers deploy.yml
  → Self-hosted runner checkout code (với submodules)
  → restore secrets từ ~/jarvis-data vào backend/
  → docker compose build (build images trên server)
  → docker compose up -d --force-recreate (deploy)
  → Health check
```

### Ghi chú quan trọng về secrets Docker

- File `backend/fastagent.secrets.docker.yaml` trong workspace runner chỉ là bản sao tạm thời.
- Nguồn thật được workflow restore mỗi lần deploy là `~/jarvis-data/fastagent.secrets.docker.yaml`.
- Nếu prod bị `401 Invalid API key` khi gọi qua CLIProxyAPI, kiểm tra file persistent này trước.
- Với kiến trúc hiện tại, `openai.api_key` trong file persistent phải là `jarvis-proxy-key`, không phải `sk-proj-...`.

---

## 8. (Tùy chọn) SSL với Nginx + Let's Encrypt

Nếu bạn có domain, setup SSL:

```bash
# Đổi Docker web port để tránh conflict với nginx host
# Sửa docker-compose.yaml: ports "3080:80" thay vì "80:80"

# Cài Nginx + Certbot
sudo apt install -y nginx certbot python3-certbot-nginx

# Tạo nginx config
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

> Thay `your-domain.com` bằng domain thật.

---

## Deploy thủ công (khi cần)

```bash
cd ~/jarvis
git pull --recurse-submodules
docker compose build
docker compose up -d --force-recreate
```

---

## Cập nhật fast-agent submodule

```bash
cd ~/jarvis
git submodule update --remote backend/fast-agent
docker compose build jarvis-backend
docker compose up -d --force-recreate jarvis-backend
```

---

## Đăng nhập bằng Passkey (Touch ID / Face ID / Windows Hello)

Sau khi vào dashboard bằng `JARVIS_API_KEY`, bạn có thể đăng ký passkey để
lần sau không phải dán key — chỉ cần chạm Touch ID là vào.

### Đăng ký lần đầu

1. Mở dashboard, nhập `JARVIS_API_KEY` từ `backend/.env` để đăng nhập như
   bình thường.
2. Vào **Settings → Authentication**.
3. Đặt label (ví dụ "MacBook Touch ID") rồi bấm **Register passkey**. Hệ
   điều hành sẽ bật prompt — chạm vân tay / camera để hoàn tất.
4. Đăng xuất, mở lại trang. Nút **"Sign in with passkey"** sẽ xuất hiện.

### Yêu cầu HTTPS

WebAuthn (passkey) chỉ hoạt động với:
- `http://localhost:*` (browser cho phép ngoại lệ cho dev)
- `https://your-domain.com` (production)

LAN IP như `http://192.168.1.50:3001` **KHÔNG** dùng được passkey — browser
sẽ từ chối ceremony. Setup HTTPS qua Caddy / nginx + Let's Encrypt /
Tailscale Funnel trước khi đăng ký.

### Passkey scope theo domain

Passkey gắn cứng vào **RP ID = hostname của lúc đăng ký**. Tức là:
- Passkey đăng ký trên `localhost` → CHỈ dùng được khi truy cập qua
  `localhost`.
- Đổi sang `jarvis.alice.com` → phải đăng ký passkey mới cho domain đó.

Đây là ràng buộc của WebAuthn spec, không phải bug. Khi chuyển sang
production domain, vào Settings → Authentication trên domain mới và bấm
"Register passkey" lần nữa.

### Cross-device (laptop + phone)

Đăng ký passkey trên từng device riêng. iCloud Keychain / Google Password
Manager / 1Password sẽ tự sync passkey nếu bạn dùng các hệ này; còn không
thì mỗi device đăng ký một lần.

### Recovery — mất passkey

Passkey không export được. Nếu mất tất cả device → cách duy nhất là
đọc lại `JARVIS_API_KEY` từ `backend/.env`, đăng nhập bằng API key, rồi
đăng ký passkey mới:

```bash
# Trên server
grep JARVIS_API_KEY backend/.env
```

API key cũng là credential cho các script (Xiaozhi voice, CLI tools) — đừng
xóa.

### Coexist với API key

Passkey không thay thế API key. Hai thứ song song:
- **Passkey**: chỉ dùng cho login browser.
- **API key trong `.env`**: dùng cho Xiaozhi, scripts, recovery.

Settings → General có nút rotate API key nếu cần.

---

## Troubleshooting

| Vấn đề | Cách fix |
|---------|----------|
| Container không start | `docker compose logs jarvis-backend` |
| Port 80 bị chiếm | `sudo lsof -i:80` → kill process hoặc đổi port |
| Permission denied (Docker) | `sudo usermod -aG docker $USER` → logout/login lại |
| Runner offline | `cd ~/actions-runner && sudo ./svc.sh status` |
| Runner không nhận job | Check runner name match `self-hosted` label |
| Disk đầy | `docker system prune -a` để xóa images cũ |
| Submodule rỗng | `git submodule update --init --recursive` |
| Backend crash loop | `docker compose logs -f jarvis-backend` → check .env và secrets |
| Passkey không show "Sign in with passkey" button | Đăng ký lần đầu từ Settings → Authentication; cần HTTPS trừ localhost |
| Passkey trên LAN IP không work | Setup HTTPS (Caddy / Tailscale Funnel / mkcert) — WebAuthn chỉ cho phép HTTPS + localhost |
| Mất hết passkey | Lấy `JARVIS_API_KEY` từ `.env`, login bằng API key, đăng ký passkey mới |
