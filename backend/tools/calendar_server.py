import os
import sys
from contextlib import redirect_stdout
from datetime import datetime, timedelta
import uuid

# Make ``services.*`` importable when this script is launched as an MCP
# subprocess (cwd=backend/, so backend/ is not on sys.path by default).
_backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from googleapiclient.discovery import build
from mcp.server.fastmcp import FastMCP

from tools.google_api_errors import format_api_error

# Initialize FastMCP server
mcp = FastMCP("calendar-server")

SCOPES = [
    'https://www.googleapis.com/auth/calendar',
]


def get_service():
    """Return a Calendar service using the shared OAuth web-flow tokens.

    See ``services.google_oauth`` for the source of truth on client config
    and tokens.  Replaces the old ``InstalledAppFlow`` path which required a
    local browser.
    """
    with redirect_stdout(sys.stderr):
        from services.google_oauth import get_credentials  # noqa: WPS433

        creds = get_credentials()
        if creds is None:
            raise RuntimeError(
                "Calendar is not connected. Open the Settings → Services tab and "
                "click 'Connect Google' to finish the OAuth flow."
            )

    return build('calendar', 'v3', credentials=creds)

@mcp.tool()
def calendar_list_events(time_min: str = None, time_max: str = None, max_results: int = 10) -> str:
    """
    List upcoming events.
    Args:
        time_min: ISO format start time (e.g. '2023-10-27T00:00:00Z'). Defaults to now.
        time_max: ISO format end time.
    """
    try:
        service = get_service()
        if not time_min:
            time_min = datetime.utcnow().isoformat() + 'Z'
        
        events_result = service.events().list(calendarId='primary', timeMin=time_min,
                                              timeMax=time_max, maxResults=max_results, singleEvents=True,
                                              orderBy='startTime').execute()
        events = events_result.get('items', [])

        if not events:
            return "No upcoming events found."

        output = []
        for event in events:
            start = event['start'].get('dateTime', event['start'].get('date'))
            summary = event.get('summary', 'No Title')
            link = event.get('htmlLink')
            output.append(f"- {start}: {summary} ({link})")
            
        return "\n".join(output)
    except Exception as e:
        return f"Error listing events: {format_api_error(e)}"

@mcp.tool()
def calendar_create_event(summary: str, start_time: str, end_time: str, location: str = "", description: str = "", use_meet: bool = False) -> str:
    """
    Create a new calendar event.
    Args:
        start_time: ISO format (e.g., '2024-12-07T14:00:00')
        end_time: ISO format
        use_meet: If True, adds a Google Meet link.
    """
    try:
        service = get_service()
        
        # 1. Conflict Check (simplified)
        # Fix: Input is expected in Local Time (no offset), so we treat it as VN time +07:00 for query
        # Attempt to handle rudimentary ISO strings
        query_start = start_time
        query_end = end_time
        
        # If no timezone info in string, append +07:00 for Vietnam
        if 'Z' not in query_start and '+' not in query_start:
            query_start += '+07:00'
        if 'Z' not in query_end and '+' not in query_end:
            query_end += '+07:00'
            
        events_result = service.events().list(calendarId='primary', timeMin=query_start, timeMax=query_end, singleEvents=True).execute()
        conflicts = events_result.get('items', [])
        if conflicts:
            conflict_details = []
            is_duplicate = False
            for e in conflicts:
                # Extract time, handling both 'dateTime' and 'date' (all-day)
                c_start = e['start'].get('dateTime', e['start'].get('date'))
                c_end = e['end'].get('dateTime', e['end'].get('date'))
                c_summary = e.get('summary', 'Busy')
                
                # Check for duplicate
                if c_summary.lower().strip() == summary.lower().strip():
                    is_duplicate = True
                
                conflict_details.append(f"'{c_summary}' ({c_start} - {c_end})")
            
            conflict_msg = ", ".join(conflict_details)
            
            if is_duplicate:
                 return f"WARNING: It seems this event already exists: {conflict_msg}. Please ask user if they want to create a DUPLICATE or if this was a mistake."

            return f"WARNING: Conflict detected with events: {conflict_msg}. Please inform user about specific conflict times and ask for confirmation."

        # 2. Prepare Event
        event = {
            'summary': summary,
            'location': location,
            'description': description,
            'start': {'dateTime': start_time, 'timeZone': 'Asia/Ho_Chi_Minh'},
            'end': {'dateTime': end_time, 'timeZone': 'Asia/Ho_Chi_Minh'},
        }

        # 3. Add Google Meet
        if use_meet:
            event['conferenceData'] = {
                'createRequest': {
                    'requestId': str(uuid.uuid4()),
                    'conferenceSolutionKey': {'type': 'hangoutsMeet'}
                }
            }

        # 4. Insert
        event = service.events().insert(calendarId='primary', body=event, conferenceDataVersion=1).execute()
        
        meet_link = event.get('conferenceData', {}).get('entryPoints', [{}])[0].get('uri', '')
        return f"[SUCCESS] Event created: {event.get('htmlLink')} \nMeet Link: {meet_link}"
    
    except Exception as e:
        return f"Error creating event: {format_api_error(e)}"


@mcp.tool()
def calendar_delete_event(event_id: str) -> str:
    """
    Delete a calendar event by its event ID.
    Args:
        event_id: The event ID to delete. Can be extracted from the event's htmlLink 
                  (the 'eid' parameter) or from the event object directly.
                  Example: From URL 'https://www.google.com/calendar/event?eid=NDE1ZzA0ZGt0Z3VjYTZjYm5zY2cxbHZwOWsgcGh1Y252MzJAbQ'
                  the event_id is the base64 decoded part before the space.
    
    Tip: To delete an event, first use calendar_list_events to find it, 
         then extract the event_id from the returned htmlLink.
    """
    try:
        import base64
        
        service = get_service()
        
        # Handle if user passed the full eid (base64 encoded)
        # The eid contains: eventId + " " + calendarId (base64 encoded)
        actual_event_id = event_id
        
        # Try to decode if it looks like base64
        try:
            # Check if it's a base64 encoded eid
            if len(event_id) > 20 and not event_id.startswith("_"):
                # Add padding if needed
                padded = event_id + "=" * (4 - len(event_id) % 4)
                decoded = base64.b64decode(padded).decode('utf-8')
                # Format: "eventId calendarId"
                parts = decoded.split(" ")
                if len(parts) >= 1:
                    actual_event_id = parts[0]
        except:
            # Not base64, use as-is
            pass
        
        service.events().delete(calendarId='primary', eventId=actual_event_id).execute()
        return f"[SUCCESS] Event deleted successfully (ID: {actual_event_id})"
    
    except Exception as e:
        return f"Error deleting event: {format_api_error(e)}"


if __name__ == "__main__":
    mcp.run()
