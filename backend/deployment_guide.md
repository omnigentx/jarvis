# Guide to Deploying the Backend on Windows & Remote Access (Free)

This guide helps you run the Jarvis Backend on your Windows laptop using Docker and access it remotely from your phone via Cloudflare Tunnel (free, with HTTPS and a fixed address).

## 1. Install Docker on Windows
If you don't have Docker yet:
1.  Download and install **Docker Desktop for Windows**: [https://www.docker.com/products/docker-desktop/](https://www.docker.com/products/docker-desktop/)
2.  After installing, open Docker Desktop and wait for it to start (the blue whale icon in the taskbar).

## 2. Prepare the Code
1.  In the project's `backend/` directory, open a Terminal (PowerShell or CMD).
2.  Make sure you have a `.env` file with all required keys (such as `GOOGLE_API_KEY`, `JARVIS_API_KEY`...). If not, copy from `.env.example`:
    ```powershell
    copy .env.example .env
    # Then open the .env file and fill in your keys
    ```

## 3. Run the Server with Docker
From the `backend/` directory, run the following command to build and start the server:

```powershell
docker compose up --build -d
```
*   `--build`: Build the latest image.
*   `-d`: Run in the background (detached) so it doesn't stop when you close the terminal window.

Check that the server is running by visiting in your browser:
*   [http://localhost:8000/docs](http://localhost:8000/docs)
*   If you see the Swagger UI page, it's working.

## 4. Create a Public URL (Cloudflare Tunnel) - Free
Cloudflare Tunnel lets you safely expose your local server to the internet without opening any modem ports. The **Zero Trust Free** plan is completely free for personal use (up to 50 users).

### Option 1: Use "Quick Tunnel" (Fastest, random URL)
Use this if you just need to test quickly and don't care about a nice-looking domain.

1.  Download `cloudflared` for Windows: [Download link from Cloudflare](https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe)
2.  Rename the downloaded file to `cloudflared.exe` and place it in a folder (e.g., `C:\tools\`).
3.  Open PowerShell, cd to that folder and run:
    ```powershell
    .\cloudflared.exe tunnel --url http://localhost:8000
    ```
4.  It will display a link like `https://random-name.trycloudflare.com`. Copy this link and paste it into your phone app.
    *   **Note:** This link will change every time you stop/restart `cloudflared`.

### Option 2: Use the Official Cloudflare Tunnel (Stable, fixed URL)
This is the best option for long-term use. You need a domain name (you can buy a cheap $1 domain or use a free domain if you can find one). If you don't have a domain, use **Option 1** or **Tailscale**.

**Assuming you already have a Cloudflare account and a domain (e.g., `myjarvis.com`):**

1.  Go to the [Cloudflare Zero Trust Dashboard](https://one.dash.cloudflare.com/).
2.  Select **Networks** > **Tunnels** > **Create a Tunnel**.
3.  Select **Cloudflared**. Give it a name (e.g., `jarvis-laptop`).
4.  It will show installation instructions ("Install and run a connector"). Select **Windows**.
    *   Copy the command it provides and run it in PowerShell (Run as Administrator) on your laptop.
    *   This command installs `cloudflared` as a Service that auto-starts on boot.
5.  Once the connector shows "Connected", click **Next**.
6.  **Public Hostnames** tab:
    *   **Subdomain**: e.g., `api` (resulting in `api.myjarvis.com`).
    *   **Domain**: select your domain.
    *   **Service**:
        *   Type: `HTTP`
        *   URL: `localhost:8000`
7.  Click **Save Tunnel**.

Now you can use `https://api.myjarvis.com` to enter into your phone app. This address stays fixed as long as your laptop is running Docker and is online.

## 5. Access the Web Dashboard

Open your browser and go to the Cloudflare address you just configured (e.g.,
`https://jarvis.omnigentx.com`). The web dashboard (Vue) is pre-built into the
`jarvis_web` container and served via nginx on port 80, with `/api/*` proxied
to the backend on port 8000.

## Useful Docker Commands
*   **View logs:** `docker compose logs -f`
*   **Restart:** `docker compose restart`
*   **Stop the server:** `docker compose down`
