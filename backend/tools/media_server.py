import json
from mcp.server.fastmcp import FastMCP
import yt_dlp

# Initialize FastMCP server
mcp = FastMCP("media-server")


@mcp.tool()
def search_youtube(query: str) -> str:
    """
    Search YouTube for music/video. Returns JSON with a 'response' field.
    IMPORTANT: You MUST reply with EXACTLY the value of the 'response' field. Do NOT modify it, do NOT add links, do NOT rewrite it. Just copy the 'response' value as your reply.
    """
    ydl_opts = {
        'format': 'bestaudio/best',
        'noplaylist': True,
        'quiet': True,
        'default_search': 'ytsearch1:',
        'extract_flat': 'in_playlist',
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            result = ydl.extract_info(query, download=False)

            if 'entries' in result and len(result['entries']) > 0:
                video = result['entries'][0]
                video_id = video['id']
                title = video['title']

                return json.dumps({
                    "response": f"Đang phát {title}. [[[PLAY: {video_id}]]]",
                    "title": title,
                    "video_id": video_id
                }, ensure_ascii=False)

            return json.dumps({"response": f"Không tìm thấy kết quả cho '{query}'.", "error": True}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"response": f"Lỗi tìm kiếm: {str(e)}", "error": True}, ensure_ascii=False)

if __name__ == "__main__":
    mcp.run()
