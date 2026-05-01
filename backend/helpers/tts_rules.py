"""TTS output normalization rule — applied ONLY to chat endpoints that
generate TTS audio. NOT applied to scheduled tasks, notifications, or any
text-only response path.

Single source of truth: import TTS_OUTPUT_RULES_VI / prepend_tts_rules
wherever a turn's response will be read aloud. Keep the rule out of the
agent's static instruction so the same agent can produce clean markdown
for UI-only paths (e.g. cron `agent_turn`).

Rule 5 from the previous in-instruction version ("avoid #/*/-/_") is
intentionally dropped: those characters are needed for markdown rendering
in the dashboard. The TTS pipeline strips markdown formatting in
`text_processing.clean_text_for_tts` so the audio remains clean.
"""

TTS_OUTPUT_RULES_VI = """\
QUY TẮC OUTPUT CHO TTS (BẮT BUỘC khi câu trả lời sẽ được đọc bằng giọng nói):
1. Đơn vị đo lường: Viết đầy đủ (kg -> ki-lô-gam, km -> ki-lô-mét, % -> phần trăm, $ -> đô la, VND/đ -> đồng, °C -> độ C).
2. Ký hiệu toán học: Viết thành lời (+ -> cộng, - -> trừ, * -> nhân, / -> chia, = -> bằng).
3. Số liệu: Số thập phân dùng "phẩy" (1.5 -> 1 phẩy 5). Số lớn viết rõ (100k -> 100 nghìn, 1M -> 1 triệu).
4. Tiếng Anh: Giữ nguyên nếu phổ biến, hoặc mở ngoặc phiên âm tiếng Việt nếu khó đọc.
"""


def prepend_tts_rules(message: str) -> str:
    """Prepend the TTS output rules to a user message.

    Use this only on the chat path — `/api/chat`, `/api/chat-stream`,
    `/api/chat-audio` — where the response is converted to speech. The
    cron scheduler MUST NOT call this: scheduled job results land in the
    notification UI as markdown and the TTS-style spelling would be
    out of place.
    """
    return f"{TTS_OUTPUT_RULES_VI}\n---\n{message}"
