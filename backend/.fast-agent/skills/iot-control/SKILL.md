---
name: iot-control
description: >
  Control IoT smart home devices. Use when user requests: start/stop robot vacuum,
  find robot, schedule cleaning, return to dock, or check robot status.
  Auto-handles Roborock 2FA via Gmail.
---

# ROBOT VACUUM CONTROL (ROBOROCK)

## Available Tools (exact MCP names)

| Intent | Tool |
|---|---|
| List all devices on account | `list_devices()` |
| Status / battery / online check | `get_robot_status(device_name=None)` |
| Consumable wear (brush, filter, dustbin) | `get_consumable_status(device_name=None)` |
| Start cleaning | `start_cleaning(device_name=None)` |
| Stop / pause | `stop_cleaning(device_name=None)` |
| Send back to dock | `return_to_dock(device_name=None)` |
| Find robot (beep) | `find_robot(device_name=None)` |
| List rooms on map | `get_room_mapping(device_name=None)` |
| Clean specific room | `clean_specific_room(room_id, device_name=None)` |
| Set suction power | `set_fan_speed(speed, device_name=None)` — speed ∈ {quiet, balanced, turbo, max} |
| Wait before re-checking status | `wait_for_seconds(seconds=5)` |

`device_name` is optional — omit to target the first / only device.

## Decision tree

User intent → tool:
- Status / location / battery → `get_robot_status()`
- Start cleaning / vacuum     → `start_cleaning()`
- Stop / pause                → `stop_cleaning()`
- Return to dock              → `return_to_dock()`
- Locate / make a sound       → `find_robot()`
- Clean a specific room       → `get_room_mapping()` → `clean_specific_room(id)`

<prerequisite>
For any ACTION (start/stop/return/clean_room), call `get_robot_status()` FIRST
to check online state. If offline → tell the user, do NOT send commands.

For a PURE STATUS question, call `get_robot_status()` directly and return the result.
</prerequisite>

<rule>
1. Always call a real MCP tool — never say "I can't access" without trying.
2. If `get_robot_status` raises AUTHENTICATION_REQUIRED → run the 2FA flow below.
3. If the device is offline → inform the user, stop.
4. Reply concisely in the user's language: "[Status] + [Action taken]".
</rule>

<2fa_flow>
When any Roborock tool returns an AUTHENTICATION_REQUIRED error:
1. Call `request_roborock_code()` — system sends a code to the Roborock account email.
2. Call `gmail_search(query="from:roborock.com newer_than:1h in:anywhere", max_results=5)`.
   - Substring match on `roborock.com` catches every subdomain (`noreply@notice-os.roborock.com`, `noreply@roborock.com`, etc.) — do NOT hard-code the full sender address.
   - `in:anywhere` covers Spam/Promotions in case Gmail mis-classifies the OTP email.
   - If still empty, retry once with `newer_than:6h`.
3. Call `gmail_read_thread(thread_id)` on the most recent hit.
4. Extract the numeric OTP from the email body.
5. Call `submit_roborock_code(code=<otp>)`.
6. Retry the original command.

Never ask the user for the OTP — read it from Gmail automatically.
</2fa_flow>

<violation>
- Saying "I can't access the robot status" without calling `get_robot_status` → VIOLATION
- Sending commands while the robot is offline → VIOLATION
- Asking the user for a 2FA code → VIOLATION (auto-read from Gmail)
- Inventing tool names (e.g. `get_status`, `read_gmail`) → VIOLATION; use the exact names above
</violation>

## ✅ Correct examples
- "What's the robot's status?" → `get_robot_status()` → "Robot is charging at the dock, battery 92%."
- "Vacuum the living room" → `get_robot_status()` → `get_room_mapping()` → `clean_specific_room(room_id=<living-room-id>)`.
- "Send it back to the dock" → `get_robot_status()` → if online `return_to_dock()` → "Robot is heading back to the dock."

Mirror the user's language in the reply.
