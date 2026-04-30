# Hướng Dẫn Thêm Agent Mới Vào Jarvis (Standard Process)

Tài liệu này quy chuẩn hóa các bước để thêm một Agent mới vào hệ thống Jarvis (`fast-agent` v0.4.40). Hãy làm theo **đúng thứ tự** này để đảm bảo Agent hoạt động trơn tru trong kiến trúc Master Agent + MAKER.

## Checklist Tóm Tắt
- [ ] **Bước 1**: Tạo file Skill mới (`.fast-agent/skills/...`).
- [ ] **Bước 2**: Định nghĩa hàm Agent mới trong `agent.py`.
- [ ] **Bước 3**: Cập nhật Prompt phân loại cho `IntentClassifier`.
- [ ] **Bước 4**: Đăng ký Agent mới vào danh sách `agents` của `Jarvis`.
- [ ] **Bước 5**: Restart server & Verify.

---

## Chi Tiết Các Bước

### Bước 1: Tạo Skill (Kỹ Năng)
Mọi Agent mới đều cần có File Skill riêng để chứa context và hướng dẫn chuyên biệt. Không viết cứng hướng dẫn vào code.

1.  **Tạo thư mục**: `backend/.fast-agent/skills/<tên-folder-skill>/`
    *   *Quy tắc*: Tên folder viết thường, dùng gạch nối (kebab-case). Ví dụ: `football-news`, `image-generation`.
2.  **Tạo file**: `SKILL.md` bên trong thư mục đó.
3.  **Nội dung mẫu**:

```markdown
---
name: <tên-skill-ngắn-gọn>
description: Mô tả kỹ năng này làm gì
---
# <Tên Skill> Instructions

## Vai trò
Bạn là chuyên gia về...

## Khả năng (Capabilities)
- [Liệt kê các khả năng]

## Quy tắc phản hồi
- [Quy tắc 1]
- [Quy tắc 2]
```

### Bước 2: Định Nghĩa Agent Trong `agent.py`
Mở file `backend/agent.py` và thêm block code định nghĩa Agent.

**Vị trí:** Đặt cùng nhóm với các "Specialized Agents".

```python
# Tìm dòng: @fast.agent
@fast.agent(
    name="<TenAgent>",          # Ví dụ: SportsAgent (PascalCase)
    instruction="Bạn là... \n\n{{agentSkills}}", # BẮT BUỘC giữ placeholder này
    skills=CORE_SKILLS + get_skills("<tên-folder-skill-ở-bước-1>"),
    model="openai.gpt-4o-mini",
    servers=["<server-1>", "<server-2>"], # Ví dụ: "serpapi", "fpl-server"
    # tools={...} # (Optional) Chỉ định rõ tools nếu cần hạn chế
)
async def <tên_hàm_agent>(prompt: str):
    pass
```

### Bước 3: Cập Nhật Intent Classifer (MAKER)
Để Jarvis biết **khi nào** thì gọi Agent này, bạn cần dạy `IntentClassifier` nhận diện ý định mới.

**Vị trí:** Tìm Agent `IntentClassifier` trong `agent.py`.

```python
@fast.agent(
    name="IntentClassifier",
    # ...
    instruction="""
    Classify the user's request...
    # ... (Các intent cũ)
    - IOT_CONTROL: ...
    # THÊM DÒNG DƯỚI ĐÂY:
    - <NEW_INTENT_NAME>: <Mô tả ngắn gọn khi nào user chọn cái này>.
    
    Respond with ONLY the category name.
    """
)
```
*Ví dụ:* `- SPORTS_NEWS: Asking for football scores, team news, fixtures.`

### Bước 4: Đăng Ký Với Master Agent (Jarvis)
Cuối cùng, báo cho "Sếp" Jarvis biết về nhân viên mới và nhiệm vụ của họ.

**Vị trí:** Tìm Agent `Jarvis` (Master Agent) ở cuối file `agent.py`.

1.  **Cập nhật Instruction Ranking/Mapping**:
    ```python
    instruction="""
    # ...
    MAPPING INTENT -> AGENT:
    # ...
    - IOT_CONTROL -> IoTAgent
    # THÊM DÒNG DƯỚI ĐÂY:
    - <NEW_INTENT_NAME> -> <TenAgent>
    # ...
    """
    ```

2.  **Thêm vào danh sách `agents`**:
    ```python
    agents=[
        "ReliableRouter",
        # ...
        "<TenAgent>",  # <--- THÊM TÊN AGENT VÀO ĐÂY (trùng với name ở Bước 2)
    ],
    ```

### Bước 5: Restart & Verify
Agent mới sẽ **không** hoạt động cho đến khi bạn khởi động lại server.

1.  **Restart Backend**:
    ```bash
    uv run uvicorn server:app --host 0.0.0.0 --reload
    ```
2.  **Test**:
    *   Chat câu lệnh kích hoạt Intent mới.
    *   Xem logs để đảm bảo `ReliableRouter` trả về đúng Intent và `Jarvis` gọi đúng Agent mới.
