---
name: team-communication
description: >
  Quy tắc giao tiếp trong team (email, meeting). Dùng khi agent cần gửi email,
  tham gia meeting, hoặc báo cáo kết quả. Tools: send_email, create_meeting.
---

# Team Communication Skill

<violation>
- KHÔNG được đọc file, browse code, hay chạy lệnh terminal để tìm hiểu communication system.
- KHÔNG dùng read_text_file, execute, grep hay bất kỳ tool nào ngoài communication tools.
- CHỈ gọi TRỰC TIẾP: `send_email`, `create_meeting`.
- Nếu vi phạm → request sẽ chậm và tốn token vô ích.
</violation>

## ⚡ Auto Status Notifications

Kết quả của team members được **auto-delivered** vào inbox khi TẤT CẢ members hoàn thành:
- Một consolidated report dạng bảng: tên, trạng thái, tóm tắt kết quả
- **KHÔNG polling.** Tập trung vào task chính, report tự đến khi mọi người xong việc.
- Nếu bạn cần kết quả chi tiết hơn, dùng `send_email` hoặc `create_meeting` để trao đổi với member cụ thể.

## 📧 Email — Async, Fire-and-Forget

- `send_email(to="Name", body="...", subject="...")` — gửi cho người cụ thể
- `send_email(to="all", body="...")` — broadcast toàn team
- Emails từ teammates được **auto-delivered** vào context — không cần poll

## Waiting for Dependencies

Nếu cần output từ teammate:
1. Gửi request: `send_email(to="Agent Name", body="Please send me [deliverable] when ready", subject="[WAITING] ...")`
2. Tiếp tục làm việc khác hoặc kết thúc task hiện tại
3. Khi teammate gửi email, bạn sẽ được auto-wake và nhận nội dung
4. Nếu cần kết quả từ nhiều members, dùng `create_meeting` để họp nhanh

## 🎙️ Meeting — Real-time Decisions

Khi nhận 🔔 MEETING INVITE → follow skill `meeting-participant` để join và phát biểu.

## Email Discipline

- **Tập trung task trước.** Chỉ email khi có deliverable, bị blocked, hoặc phát hiện critical issue. KHÔNG email chỉ để update status hay acknowledge.
- **Ngắn gọn nhưng đầy đủ.** Include đủ context. Dùng subject prefix: `[DONE]`, `[BLOCKED]`, `[BUG]`, `[REVIEW]`, `[DELIVERABLE]`, `[WAITING]`.
- **Dùng CC hạn chế.** Chỉ CC người thực sự cần biết.
- **KHÔNG reply chỉ để acknowledge.** Dùng `no_reply=True` cho FYI messages.
- **Tránh email ping-pong.** Nếu cần trao đổi qua lại, tạo meeting thay vì email.

## Completion Rules (BẮT BUỘC)

TRƯỚC KHI idle hoặc hoàn thành công việc, BẮT BUỘC phải:
1. Gửi báo cáo `[DONE]` cho PM: `send_email(to="Linh - PM", subject="[DONE] <tóm tắt deliverables>", body="<danh sách deliverables, files, outcomes, open items>")`
2. KHÔNG BAO GIỜ idle mà không gửi báo cáo — đây là **BẮT BUỘC**
