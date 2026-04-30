# Hướng Dẫn Deploy Backend lên Windows & Remote Access (Free)

Hướng dẫn này giúp bạn chạy Jarvis Backend trên laptop Windows của mình bằng Docker và truy cập từ xa qua điện thoại bằng Cloudflare Tunnel (miễn phí, có HTTPS và địa chỉ cố định).

## 1. Cài Đặt Docker trên Windows
Nếu bạn chưa có Docker:
1.  Tải và cài đặt **Docker Desktop for Windows**: [https://www.docker.com/products/docker-desktop/](https://www.docker.com/products/docker-desktop/)
2.  Sau khi cài đặt, mở Docker Desktop và đợi nó khởi động (icon cá voi màu xanh ở taskbar).

## 2. Chuẩn Bị Code
1.  Tại thư mục `backend/` của dự án, mở Terminal (PowerShell hoặc CMD).
2.  Đảm bảo bạn đã có file `.env` với đầy đủ key (như `GOOGLE_API_KEY`, `JARVIS_API_KEY`...). Nếu chưa, copy từ `.env.example`:
    ```powershell
    copy .env.example .env
    # Sau đó mở file .env và điền key vào
    ```

## 3. Chạy Server Bằng Docker
Tại thư mục `backend/`, chạy lệnh sau để build và khởi động server:

```powershell
docker compose up --build -d
```
*   `--build`: Build lại image mới nhất.
*   `-d`: Chạy ngầm (detach) để không bị tắt khi đóng cửa sổ terminal.

Kiểm tra xem server đã chạy chưa bằng cách vào trình duyệt:
*   [http://localhost:8000/docs](http://localhost:8000/docs)
*   Nếu thấy trang Swagger UI là thành công.

## 4. Tạo Public URL (Cloudflare Tunnel) - Miễn Phí
Cloudflare Tunnel cho phép bạn "public" server local ra internet một cách an toàn mà không cần mở port modem. Gói **Zero Trust Free** hoàn toàn miễn phí cho cá nhân (lên đến 50 users).

### Cách 1: Dùng "Quick Tunnel" (Nhanh nhất, URL ngẫu nhiên)
Nếu bạn chỉ cần test nhanh và không quan trọng tên miền đẹp.

1.  Tải `cloudflared` cho Windows: [Link tải từ Cloudflare](https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe)
2.  Đổi tên file tải về thành `cloudflared.exe` và để vào một thư mục (ví dụ `C:\tools\`).
3.  Mở PowerShell, cd vào thư mục đó và chạy:
    ```powershell
    .\cloudflared.exe tunnel --url http://localhost:8000
    ```
4.  Nó sẽ hiện ra một đường link như `https://random-name.trycloudflare.com`. Copy link này dán vào app điện thoại.
    *   **Lưu ý:** Link này sẽ đổi mỗi khi bạn tắt/bật lại `cloudflared`.

### Cách 2: Dùng Cloudflare Tunnel Chính Thức (Ổn định, URL cố định)
Đây là cách tốt nhất để dùng lâu dài. Bạn cần một tên miền (có thể mua tên miền rẻ 1$ hoặc dùng tên miền free nếu kiếm được). Nếu không có tên miền, hãy dùng **Cách 1** hoặc **Tailscale**.

**Giả sử bạn đã có tài khoản Cloudflare và một tên miền (ví dụ: `myjarvis.com`):**

1.  Vào [Cloudflare Zero Trust Dashboard](https://one.dash.cloudflare.com/).
2.  Chọn **Networks** > **Tunnels** > **Create a Tunnel**.
3.  Chọn **Cloudflared**. Đặt tên (ví dụ: `jarvis-laptop`).
4.  Nó sẽ hiện hướng dẫn cài đặt ("Install and run a connector"). Chọn **Windows**.
    *   Copy đoạn lệnh nó đưa ra và chạy trong PowerShell (Run as Administrator) trên laptop.
    *   Lệnh này sẽ cài `cloudflared` như một Service, tự chạy khi bật máy.
5.  Sau khi connector hiện "Connected", bấm **Next**.
6.  Tab **Public Hostnames**:
    *   **Subdomain**: ví dụ `api` (để thành `api.myjarvis.com`).
    *   **Domain**: chọn domain của bạn.
    *   **Service**:
        *   Type: `HTTP`
        *   URL: `localhost:8000`
7.  Bấm **Save Tunnel**.

Bây giờ bạn có thể dùng `https://api.myjarvis.com` để nhập vào app điện thoại. Địa chỉ này cố định mãi mãi miễn là laptop bạn đang chạy Docker và có internet.

## 5. Truy Cập Web Dashboard

Mở trình duyệt và vào địa chỉ Cloudflare bạn vừa cấu hình (ví dụ
`https://jarvis.omnigentx.com`). Web dashboard (Vue) đã được build sẵn vào
container `jarvis_web` và serve qua nginx ở port 80, có proxy `/api/*`
sang backend ở port 8000.

## Các Lệnh Docker Hữu Ích
*   **Xem logs:** `docker compose logs -f`
*   **Khởi động lại:** `docker compose restart`
*   **Tắt server:** `docker compose down`
