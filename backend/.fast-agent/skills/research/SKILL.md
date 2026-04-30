---
name: research
description: >
  Tìm kiếm và tổng hợp thông tin từ internet. Dùng khi user hỏi về tin tức, sự kiện,
  hoặc cần deep research tổng hợp. Ưu tiên: serpapi → ScraplingServer → chrome-devtools.
---

# NGHIÊN CỨU VÀ TÌM KIẾM

## Skill này vs `scrape-web`

| Tình huống | Dùng skill nào |
|-----------|---------------|
| Tìm kiếm thông tin tổng hợp | **research** (skill này) |
| Truy cập URL cụ thể user đưa | `scrape-web` |
| Trang bị chặn/cần bypass | `scrape-web` |

## Thứ tự ưu tiên

```
1. serpapi → Nhanh, ổn định. Luôn dùng gl=vn, hl=vi
   ↓ Không đủ kết quả?
2. ScraplingServer get → Truy cập trực tiếp URL từ kết quả serpapi
   ↓ Bị 403?
3. ScraplingServer fetch/stealthy_fetch → Bypass anti-bot
   ↓ Cần tương tác (login, click)?
4. chrome-devtools → PHƯƠNG ÁN CUỐI CÙNG (rất chậm)
```

## Quy tắc
- Luôn trích dẫn nguồn khi có thể
- Tổng hợp từ nhiều nguồn nếu kết quả serpapi phong phú
- **KHÔNG** dùng ScraplingServer cho search queries — chỉ dùng để truy cập URL cụ thể
- **KHÔNG** dùng chrome-devtools trừ khi bắt buộc phải tương tác (click, login)
