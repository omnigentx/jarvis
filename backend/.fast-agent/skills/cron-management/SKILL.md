---
name: cron-management
description: >
  Quản lý tác vụ định kỳ (cron jobs). Dùng khi user muốn: tạo/sửa/xóa nhắc nhở, lịch định kỳ,
  tác vụ tự động (agent_turn). PHẢI gọi get_current_time trước khi tạo one-shot job để biết ngày giờ.
---

# Cron Management Skill

<violation>
- KHÔNG được đọc file, browse code, hay chạy lệnh terminal để tìm hiểu cron system.
- KHÔNG dùng read_text_file, execute, grep hay bất kỳ tool nào ngoài cron tools.
- CHỈ gọi TRỰC TIẾP: `cron_create`, `cron_list`, `cron_update`, `cron_delete`, `get_current_time`.
- Nếu vi phạm → request sẽ chậm và tốn token vô ích.
</violation>

Bạn có 4 tools sẵn sàng để gọi NGAY, KHÔNG cần tìm hiểu thêm: `cron_create`, `cron_list`, `cron_update`, `cron_delete`.

## Quy Tắc Cron Expression (BẮT BUỘC)

Cron expression có 5 fields: `minute hour day_of_month month day_of_week`

### Ví dụ phổ biến:
- `0 9 * * *` → Mỗi ngày 9h sáng
- `0 9 * * 1-5` → 9h sáng thứ 2 đến thứ 6
- `*/30 * * * *` → Mỗi 30 phút
- `0 */4 * * *` → Mỗi 4 tiếng
- `0 7 27 4 *` → Ngày 27/4 hàng năm lúc 7h sáng
- `0 15 31 3 *` + one_shot=true → Một lần lúc 3h chiều ngày 31/3

## Phân Biệt Calendar (BẮT BUỘC)

- **Dương lịch (solar)** — MẶC ĐỊNH: dùng cho mọi ngày/tháng thông thường
- **Âm lịch (lunar)**: CHỈ dùng khi user đề cập: mùng, rằm, Tết, Vu Lan, Trung Thu, âm lịch, tháng Chạp, giỗ (theo âm lịch)
  - `0 7 15 * *` + calendar_type="lunar" → Rằm hàng tháng
  - `0 6 1 * *` + calendar_type="lunar" → Mùng 1 hàng tháng
  - `0 7 10 3 *` + calendar_type="lunar" → 10/3 âm lịch (Giỗ tổ)

## Phân Biệt Exec Mode (BẮT BUỘC)

| Dấu hiệu | Exec Mode | Ý nghĩa |
|-----------|-----------|---------|
| "nhắc tôi", "đừng quên", "reminder" | `reminder` | Gửi notification text cho user |
| "tổng hợp", "phân tích", "check", "crawl", "tìm" | `agent_turn` | AI tự động thực hiện task |

- `exec_mode = "reminder"` → `exec_payload` = nội dung nhắc nhở. **KHÔNG cần** `exec_agent`.
- `exec_mode = "agent_turn"` → `exec_payload` = **prompt thực thi trực tiếp**, `exec_agent` = tên agent (BẮT BUỘC). Danh sách agent hợp lệ:
  - `jarvis` — tổng hợp, phân tích, trả lời câu hỏi phức tạp
  - `ResearchAgent` — tìm kiếm web, tin tức
  - `FinanceAgent` — giá cổ phiếu, vàng, crypto

### ⚠️ Viết exec_payload cho agent_turn (QUAN TRỌNG)

`exec_payload` là prompt sẽ được gửi cho agent khi cron trigger — nó phải là **mệnh lệnh thực thi trực tiếp**.

**KHÔNG** copy nguyên văn yêu cầu scheduling của user (ví dụ: "Mỗi ngày lúc 7h sáng, hãy...").
**HÃY** viết lại thành prompt hành động:

| ❌ SAI (copy scheduling request) | ✅ ĐÚNG (prompt thực thi) |
|---|---|
| "Mỗi ngày lúc 7h sáng, hãy kiểm tra thời tiết tại HN" | "Kiểm tra thời tiết hôm nay tại Gia Lâm, HN và gửi thông báo cho user" |
| "Hàng ngày 8h tổng hợp tin AI" | "Tổng hợp tin tức AI nổi bật trong ngày, trình bày dạng bullet points" |
| "Nhắc tôi mỗi sáng thứ 7 log TAS" | (dùng exec_mode=reminder, không phải agent_turn) |

## Pause/Resume

- Tạm dừng: `cron_update(job_id="...", status="paused")`
- Tiếp tục: `cron_update(job_id="...", status="active")`

## Luôn Confirm Với User

Sau khi tạo job, luôn xác nhận lại:
- Tên job
- Lịch chạy (giải thích bằng tiếng Việt)
- Mode (nhắc nhở hay AI thực thi)
- Lần chạy tiếp theo
