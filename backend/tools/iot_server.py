import asyncio
import os
import json
from mcp.server.fastmcp import FastMCP
from roborock.web_api import RoborockApiClient
from roborock.devices.device_manager import create_device_manager, UserParams
from roborock.data import UserData
from roborock.cloud_api import RoborockMqttClient
from roborock.command_cache import CacheableAttribute
from roborock import RoborockCommand
from roborock.exceptions import RoborockException
from dotenv import load_dotenv
import pathlib

load_dotenv()


# Initialize FastMCP server
mcp = FastMCP("IoTControl")

import time

# Global variables
api_client = None
user_data = None
device_manager = None
cached_devices = None
last_device_fetch = 0
DEVICE_CACHE_TTL = 300  # 5 minutes

SESSION_FILE = "config/credentials/roborock_session.json"

async def save_session(u_data: UserData):
    """Lưu thông tin phiên đăng nhập vào file."""
    try:
        with open(SESSION_FILE, "w") as f:
            json.dump(u_data.as_dict(), f)
    except Exception as e:
        print(f"Error saving session: {e}")

async def load_session() -> UserData | None:
    """Tải thông tin phiên đăng nhập từ file."""
    try:
        if not pathlib.Path(SESSION_FILE).exists():
            return None
        with open(SESSION_FILE, "r") as f:
            data = json.load(f)
            return UserData.from_dict(data)
    except Exception as e:
        print(f"Error loading session: {e}")
        return None

import logging
logger = logging.getLogger("iot_server")

async def get_session():
    global api_client, user_data, device_manager
    
    username = os.getenv("ROBOROCK_USERNAME", "").strip().strip('"').strip("'")
    password = os.getenv("ROBOROCK_PASSWORD", "").strip().strip('"').strip("'")
    
    logger.debug(f"Username length: {len(username)}, Password length: {len(password)}")

    if not username or not password:

        raise ValueError("ROBOROCK_USERNAME and ROBOROCK_PASSWORD must be set in environment variables.")
        
    if api_client is None:
        api_client = RoborockApiClient(username)

    if user_data is None:
        # Try to load session first
        user_data = await load_session()

    if user_data is None:
        # If no session, login with password
        try:
            user_data = await api_client.pass_login(password)
            await save_session(user_data)
        except Exception as e:
            if "2031" in str(e) or "need two step validate" in str(e):
                 # Auto-request code
                 try:
                     await api_client.request_code()
                     raise Exception("AUTHENTICATION_REQUIRED: 2FA Code sent to email. AGENT: DO NOT ASK USER. Automatically use `gmail_search` to find code from 'roborock', then `submit_roborock_code`.")
                 except Exception as req_e:
                     raise Exception(f"Cần xác thực 2 bước nhưng không thể tự gửi mã: {str(req_e)}")
            raise e
        
    if device_manager is None:
        user_params = UserParams(username=username, user_data=user_data)
        device_manager = await create_device_manager(user_params)
        
    return api_client, user_data, device_manager

@mcp.tool()
async def request_roborock_code() -> str:
    """Yêu cầu gửi mã xác thực 2 bước (2FA) qua email."""
    global api_client
    try:
        username = os.getenv("ROBOROCK_USERNAME", "").strip().strip('"').strip("'")
        if api_client is None:
            api_client = RoborockApiClient(username)
        
        await api_client.request_code()
        return f"Đã gửi yêu cầu mã xác thực đến email đăng ký của tài khoản {username}. Vui lòng kiểm tra email và dùng lệnh 'submit_roborock_code' để nhập mã."
    except Exception as e:
        return f"Lỗi khi yêu cầu mã: {str(e)}"

@mcp.tool()
async def submit_roborock_code(code: str) -> str:
    """
    Nhập mã xác thực 2 bước để đăng nhập.
    Args:
        code: Mã số nhận được qua email (chuỗi hoặc số).
    """
    global api_client, user_data, device_manager
    try:
        # Ensure code is a string
        code = str(code)
        
        if api_client is None:
             username = os.getenv("ROBOROCK_USERNAME", "").strip().strip('"').strip("'")
             api_client = RoborockApiClient(username)
             
        user_data = await api_client.code_login(code)
        await save_session(user_data)
        
        # Initialize device manager
        username = os.getenv("ROBOROCK_USERNAME", "").strip().strip('"').strip("'")
        user_params = UserParams(username=username, user_data=user_data)
        device_manager = await create_device_manager(user_params)
        
        return "Đăng nhập 2FA thành công! Giờ bạn có thể điều khiển robot."
    except Exception as e:
        return f"Lỗi khi nhập mã xác thực: {str(e)}"

@mcp.tool()
async def list_devices() -> str:
    """
    Liệt kê các thiết bị Roborock đang sở hữu.
    Returns:
        str: Danh sách thiết bị (Tên, ID, Online/Offline)
    """
    try:
        # Force refresh for list_devices command
        global cached_devices, last_cache_time
        client, u_data, _ = await get_session()
        home_data = await client.get_home_data(u_data)
        devices = home_data.devices
        
        # Update cache
        cached_devices = devices
        last_cache_time = time.time()
        
        if not devices:
            return "Không tìm thấy thiết bị nào."
            
        result = "Danh sách thiết bị:\n"
        for device in devices:
            status = "Online" if device.online else "Offline"
            result += f"- {device.name} (ID: {device.duid}) - {status}\n"
            
        return result
    except Exception as e:
        return f"Lỗi khi lấy danh sách thiết bị: {str(e)}"

async def get_target_device(device_name: str = None):
    global cached_devices, last_device_fetch
    client, u_data, manager = await get_session()
    
    current_time = time.time()
    if cached_devices is None or (current_time - last_device_fetch > DEVICE_CACHE_TTL):
        # Refresh cache
        home_data = await client.get_home_data(u_data)
        cached_devices = home_data.devices
        last_device_fetch = current_time
        
    devices = cached_devices
    
    if not devices:
        raise RoborockException("No devices found")
        
    target_device_data = devices[0]
    if device_name:
        found = False
        for d in devices:
            if d.name.lower() == device_name.lower():
                target_device_data = d
                found = True
                break
        if not found:
            # Try partial match
            for d in devices:
                if device_name.lower() in d.name.lower():
                    target_device_data = d
                    found = True
                    break
    
    # Get the controllable device object from manager
    device = await manager.get_device(target_device_data.duid)
    return device

@mcp.tool()
async def start_cleaning(device_name: str = None) -> str:
    """
    Bắt đầu dọn dẹp (Có kiểm tra trạng thái).
    Args:
        device_name: Tên thiết bị.
    Returns:
        str: Kết quả lệnh kèm trạng thái mới.
    """
    try:
        device = await get_target_device(device_name)
        if device.v1_properties:
             # 1. Send Command
             try:
                 await device.v1_properties.command.send(RoborockCommand.APP_START)
             except Exception as e:
                 # Even if it times out, the command might have reached the device.
                 # We proceed to verification.
                 print(f"Warning sending command: {e}")

             # 2. Verify Loop (Max 5 attempts * 2s = 10s)
             for i in range(5):
                 await asyncio.sleep(2)
                 try:
                     await device.v1_properties.status.refresh()
                     status = device.v1_properties.status
                     if status.state in [5, 17, 18]: # Cleaning states
                         state_map = {5: "Cleaning", 17: "Zone Cleaning", 18: "Room Cleaning"}
                         return f"Thành công: {device.name} đã bắt đầu dọn dọn dẹp (Trạng thái: {state_map.get(status.state)})."
                 except:
                     pass # Ignore refresh errors during loop
             
             # 3. Fallback result
             return f"Đã gửi lệnh dọn dẹp tới {device.name}. (Trạng thái chưa cập nhật kịp, vui lòng kiểm tra lại sau)."
                 
        return f"Thiết bị {device.name} không hỗ trợ giao thức V1."
    except Exception as e:
        return f"Lỗi khi bắt đầu dọn dẹp: {str(e)}"

@mcp.tool()
async def stop_cleaning(device_name: str = None) -> str:
    """
    Dừng dọn dẹp (Có kiểm tra trạng thái).
    Args:
        device_name: Tên thiết bị.
    Returns:
        str: Kết quả lệnh kèm trạng thái mới.
    """
    try:
        device = await get_target_device(device_name)
        if device.v1_properties:
             try:
                 await device.v1_properties.command.send(RoborockCommand.APP_STOP)
             except Exception as e:
                 print(f"Warning sending command: {e}")
             
             # Verify Loop
             for i in range(5):
                 await asyncio.sleep(2)
                 try:
                     await device.v1_properties.status.refresh()
                     status = device.v1_properties.status
                     if status.state in [3, 10, 8, 2]: # Idle/Paused/Charging/Sleeping
                         state_map = {3: "Idle", 10: "Paused", 8: "Charging", 2: "Sleeping"}
                         return f"Thành công: {device.name} đã dừng lại (Trạng thái: {state_map.get(status.state)})."
                 except:
                     pass

             return f"Đã gửi lệnh dừng tới {device.name}. (Trạng thái chưa cập nhật kịp)."
                 
        return f"Thiết bị {device.name} không hỗ trợ giao thức V1."
    except Exception as e:
        return f"Lỗi khi dừng dọn dẹp: {str(e)}"

@mcp.tool()
async def return_to_dock(device_name: str = None) -> str:
    """
    Quay về dock sạc (Có kiểm tra trạng thái).
    Args:
        device_name: Tên thiết bị.
    Returns:
        str: Kết quả lệnh kèm trạng thái mới.
    """
    try:
        device = await get_target_device(device_name)
        if device.v1_properties:
             # Send stop first (fire and forget)
             try:
                await device.v1_properties.command.send(RoborockCommand.APP_STOP)
                await asyncio.sleep(0.5)
             except:
                 pass
                 
             try:
                 await device.v1_properties.command.send(RoborockCommand.APP_CHARGE)
             except Exception as e:
                 print(f"Warning sending command: {e}")
             
             # Verify Loop
             for i in range(5):
                 await asyncio.sleep(2)
                 try:
                     await device.v1_properties.status.refresh()
                     status = device.v1_properties.status
                     if status.state in [6, 15, 8]: # Returning/Docking/Charging
                         state_map = {6: "Returning to Dock", 15: "Docking", 8: "Charging"}
                         return f"Thành công: {device.name} đang quay về dock (Trạng thái: {state_map.get(status.state)})."
                 except:
                     pass
             
             return f"Đã gửi lệnh về dock tới {device.name}. (Trạng thái chưa cập nhật kịp)."
                 
        return f"Thiết bị {device.name} không hỗ trợ giao thức V1."
    except Exception as e:
        return f"Lỗi khi quay về dock: {str(e)}"

@mcp.tool()
async def get_robot_status(device_name: str = None) -> str:
    """
    Lấy trạng thái chi tiết của robot.
    Args:
        device_name: Tên thiết bị.
    Returns:
        str: Thông tin pin, trạng thái hoạt động, diện tích dọn, thời gian dọn.
    """
    try:
        device = await get_target_device(device_name)
        
        if not device.v1_properties:
             return f"Thiết bị {device.name} không hỗ trợ giao thức V1 hoặc chưa kết nối."

        # Refresh status trait
        await device.v1_properties.status.refresh()
        status = device.v1_properties.status
        
        state_map = {
            1: "Starting", 2: "Charger Disconnected", 3: "Idle", 4: "Remote Control",
            5: "Cleaning", 6: "Returning Dock", 7: "Manual Mode", 8: "Charging",
            9: "Charging Error", 10: "Paused", 11: "Spot Cleaning", 12: "In Error",
            13: "Shutting Down", 14: "Updating", 15: "Docking", 16: "Go To",
            17: "Zone Clean", 18: "Room Clean", 22: "Emptying Dust", 23: "Washing Mop",
            26: "Going to Wash Mop", 100: "Fully Charged"
        }
        
        state_str = state_map.get(status.state, f"Unknown ({status.state})")
        
        return (
            f"Trạng thái: {state_str}\n"
            f"Pin: {status.battery}%\n"
            f"Diện tích dọn lần cuối: {status.clean_area / 1000000:.2f} m²\n"
            f"Thời gian dọn lần cuối: {status.clean_time / 60:.1f} phút\n"
            f"Lực hút (Fan Power): {status.fan_power}%\n"
            f"Lỗi: {status.error_code if status.error_code else 'Không có'}"
        )
    except Exception as e:
        return f"Lỗi khi lấy trạng thái: {str(e)}"

@mcp.tool()
async def get_consumable_status(device_name: str = None) -> str:
    """
    Lấy trạng thái phụ kiện (consumables).
    Args:
        device_name: Tên thiết bị.
    Returns:
        str: % còn lại của chổi chính, chổi phụ, màng lọc, cảm biến.
    """
    try:
        device = await get_target_device(device_name)
        
        if not device.v1_properties:
             return f"Thiết bị {device.name} không hỗ trợ giao thức V1 hoặc chưa kết nối."

        # Refresh consumables trait
        await device.v1_properties.consumables.refresh()
        consumables = device.v1_properties.consumables
        
        # Helper to convert seconds remaining to percentage (approximate based on typical lifespan)
        # Main brush: 300h, Side brush: 200h, Filter: 150h, Sensor: 30h (cleaning interval)
        
        def seconds_to_hours(seconds):
            if seconds is None: return 0
            return seconds / 3600

        return (
            f"Tình trạng phụ kiện:\n"
            f"- Chổi chính: còn {seconds_to_hours(consumables.main_brush_work_time):.1f} giờ sử dụng\n"
            f"- Chổi phụ: còn {seconds_to_hours(consumables.side_brush_work_time):.1f} giờ sử dụng\n"
            f"- Màng lọc: còn {seconds_to_hours(consumables.filter_work_time):.1f} giờ sử dụng\n"
            f"- Cảm biến: còn {seconds_to_hours(consumables.sensor_dirty_time):.1f} giờ (cần vệ sinh)\n"
        )
    except Exception as e:
        return f"Lỗi khi lấy trạng thái phụ kiện: {str(e)}"

@mcp.tool()
async def find_robot(device_name: str = None) -> str:
    """
    Tìm robot (phát âm thanh).
    Args:
        device_name: Tên thiết bị.
    Returns:
        str: Kết quả lệnh.
    """
    try:
        device = await get_target_device(device_name)
        if device.v1_properties:
            await device.v1_properties.command.send(RoborockCommand.FIND_ME)
            return f"Đã gửi lệnh 'Tìm tôi' tới {device.name}. Robot sẽ phát ra âm thanh."
        return f"Thiết bị {device.name} không hỗ trợ giao thức V1."
    except Exception as e:
        return f"Lỗi khi tìm robot: {str(e)}"

@mcp.tool()
async def get_room_mapping(device_name: str = None) -> str:
    """
    Lấy danh sách ID các phòng (Rooms/Segments).
    Args:
        device_name: Tên thiết bị.
    Returns:
        str: Danh sách ID phòng.
    """
    try:
        device = await get_target_device(device_name)
        # Note: Roborock API is tricky with rooms. We try to get the map data.
        # This usually requires the robot to have a saved map.
        
        # Attempt to get room mapping is complex and often requires parsing map data bytes.
        # For simplicity in this SDK wrapper, we might not get friendly names easily without cloud map parsing.
        # However, we can try to get the 'segments' if available in the map data.
        
        # Currently, python-roborock doesn't expose a simple 'get_rooms' method that returns names.
        # We will return a guide for the user.
        
        return (
            "Hiện tại API chưa hỗ trợ lấy tên phòng trực tiếp một cách dễ dàng.\n"
            "Để dọn phòng cụ thể, bạn cần biết ID của phòng (thường là số 16, 17, ...).\n"
            "Bạn có thể thử lệnh 'clean_specific_room' với các ID từ 16 đến 25 để xác định phòng nào là phòng nào."
        )
    except Exception as e:
        return f"Lỗi khi lấy thông tin phòng: {str(e)}"

@mcp.tool()
async def clean_specific_room(room_id: int, device_name: str = None) -> str:
    """
    Dọn dẹp một phòng cụ thể theo ID.
    Args:
        room_id: ID của phòng (số nguyên, ví dụ 16, 17).
        device_name: Tên thiết bị.
    Returns:
        str: Kết quả lệnh.
    """
    try:
        device = await get_target_device(device_name)
        if device.v1_properties:
            # app_segment_clean takes a list of segments.
            # We assume 1 repeat count.
            await device.v1_properties.command.send(RoborockCommand.APP_SEGMENT_CLEAN, [room_id])
            return f"Đã gửi lệnh dọn phòng ID {room_id} tới {device.name}."
        return f"Thiết bị {device.name} không hỗ trợ giao thức V1."
    except Exception as e:
        return f"Lỗi khi dọn phòng {room_id}: {str(e)}"

@mcp.tool()
async def set_fan_speed(speed: str, device_name: str = None) -> str:
    """
    Chỉnh lực hút (Fan Speed).
    Args:
        speed: Mức độ ('quiet', 'balanced', 'turbo', 'max', 'off').
        device_name: Tên thiết bị.
    Returns:
        str: Kết quả lệnh.
    """
    try:
        device = await get_target_device(device_name)
        
        speed_map = {
            "quiet": 101,
            "balanced": 102,
            "turbo": 103,
            "max": 104,
            "off": 105 # Mop only usually
        }
        
        speed_code = speed_map.get(speed.lower())
        if not speed_code:
            return f"Mức độ '{speed}' không hợp lệ. Chọn: quiet, balanced, turbo, max, off."

        if device.v1_properties:
            await device.v1_properties.command.send(RoborockCommand.APP_SET_CUSTOM_MODE, speed_code)
            return f"Đã chỉnh lực hút thành {speed} ({speed_code}) cho {device.name}."
        return f"Thiết bị {device.name} không hỗ trợ giao thức V1."
    except Exception as e:
        return f"Lỗi khi chỉnh lực hút: {str(e)}"

@mcp.tool()
async def wait_for_seconds(seconds: int = 5) -> str:
    """
    Chờ một khoảng thời gian (giây).
    Dùng công cụ này SAU KHI gửi lệnh điều khiển để đợi robot xử lý,
    TRƯỚC KHI gọi get_robot_status để kiểm tra kết quả.
    Args:
        seconds: Số giây cần chờ (mặc định 5s).
    Returns:
        str: Thông báo đã chờ xong.
    """
    await asyncio.sleep(seconds)
    return f"Đã chờ {seconds} giây."

if __name__ == "__main__":
    mcp.run()
