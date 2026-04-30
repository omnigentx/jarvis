---
name: audio-reading
description: >
  Tìm và phát truyện/sách nói từ thư viện local. Dùng khi user yêu cầu: đọc truyện, phát audio truyện,
  nghe tiếp chương. Bắt buộc list truyện trước để match tên chính xác.
  Output chứa tag [[[READ_LOCAL]]] hoặc [[[READ_STORY]]].
---

<critical_rules>
<rule>KHÔNG BAO GIỜ đọc hoặc tóm tắt nội dung truyện. Hệ thống sẽ tự đọc và phát audio.</rule>
<rule>Luôn gọi local_list_stories() TRƯỚC để lấy danh sách truyện có trong thư viện.</rule>
<rule>So sánh tên user yêu cầu với danh sách, chọn tên truyện CHÍNH XÁC nhất (user có thể đọc sai/ASR nhầm).</rule>
<rule>CHỈ trả 1 câu ngắn + tag từ kết quả tool. Không giải thích thêm.</rule>
</critical_rules>

<workflow>
## Bước 1: Xác định truyện
1. Gọi `local_list_stories()` → nhận danh sách tên truyện trong thư viện
2. So sánh tên user yêu cầu với danh sách (không cần khớp hoàn toàn, chọn gần nhất)
3. Nếu không chắc → hỏi user xác nhận

## Bước 2: Lấy chương
1. Gọi `local_list_chapters(tên_truyện_chính_xác)` → danh sách chương
2. Tìm chương user yêu cầu hoặc chương tiếp theo

## Bước 3: Phát
1. Gọi `find_story_chapter(tên_truyện, số_chương)` → nhận tag
2. Response format: "Đang phát [tên] chương [X]. [[[TAG]]]"
</workflow>

<output_format>
<example>Đang phát Tiên Nghịch chương 5. [[[READ_LOCAL: Tiên Nghịch|005_Tiên Nghịch.txt]]]</example>
<example>Đang phát Tru Tiên chương 10. [[[READ_STORY: https://truyenfull.vision/tru-tien/chuong-10/]]]</example>
</output_format>

<matching_tips>
- User nói "tiên nghịch" → match "Tiên Nghịch" 
- User nói "tru tiên 2" → match "Tru Tiên II"
- User nói "vũ động" → match "Vũ Động Càn Khôn"
- Không khớp hoàn toàn cũng được, chọn story gần nhất trong danh sách
</matching_tips>
