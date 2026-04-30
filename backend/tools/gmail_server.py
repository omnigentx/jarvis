import os
import base64
import sys
from contextlib import redirect_stdout
from datetime import datetime
from typing import List, Optional
from email.message import EmailMessage

# Make ``services.*`` importable when this script is launched as an MCP
# subprocess (cwd=backend/, so backend/ is not on sys.path by default).
_backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from googleapiclient.discovery import build
from mcp.server.fastmcp import FastMCP

from tools.google_api_errors import format_api_error

# Initialize FastMCP server
mcp = FastMCP("gmail-server")

# Scopes declared so error messages stay informative; the actual scope set is
# owned by ``services.google_oauth.DEFAULT_SCOPES``.
SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.modify',
    'https://www.googleapis.com/auth/gmail.compose',
]


def get_service():
    """Return a Gmail service backed by the shared OAuth web-flow tokens.

    The old ``InstalledAppFlow`` path popped a browser on the MCP host,
    which doesn't work in Docker or remote deployments.  We now defer to
    ``services.google_oauth`` which is driven by the dashboard's Settings
    UI and persists tokens encrypted in the DB.
    """
    # Route output to stderr — the Google client libs occasionally log to
    # stdout which would corrupt the MCP JSON-RPC stream over stdin/stdout.
    with redirect_stdout(sys.stderr):
        # Lazy import so this module is still importable if the backend
        # package isn't on sys.path (e.g. during standalone tool testing).
        from services.google_oauth import get_credentials  # noqa: WPS433

        creds = get_credentials()
        if creds is None:
            raise RuntimeError(
                "Gmail is not connected. Open the Settings → Services tab and "
                "click 'Connect Google' to finish the OAuth flow."
            )

    return build('gmail', 'v1', credentials=creds)

@mcp.tool()
def gmail_list_labels() -> str:
    """List all labels in the user's mailbox."""
    try:
        service = get_service()
        results = service.users().labels().list(userId='me').execute()
        labels = results.get('labels', [])
        
        if not labels:
            return "No labels found."
        
        return "\n".join([f"- {label['name']} (ID: {label['id']})" for label in labels])
    except Exception as e:
        return f"Error listing labels: {format_api_error(e)}"

@mcp.tool()
def gmail_search(query: str, max_results: int = 10) -> str:
    """
    Search for emails using Gmail search syntax.
    Examples:
    - "is:unread" (Unread emails)
    - "from:boss@example.com" (From specific sender)
    - "subject:invoice has:attachment"
    """
    try:
        service = get_service()
        results = service.users().messages().list(userId='me', q=query, maxResults=max_results).execute()
        messages = results.get('messages', [])
        
        if not messages:
            return "No messages found."
        
        output = []
        for msg in messages:
            # Batch getting headers would be more efficient but simple get is fine for small N
            msg_detail = service.users().messages().get(userId='me', id=msg['id'], format='metadata', metadataHeaders=['From', 'Subject', 'Date']).execute()
            headers = {h['name']: h['value'] for h in msg_detail['payload']['headers']}
            snippet = msg_detail.get('snippet', '')
            output.append(f"ID: {msg['id']}\nFrom: {headers.get('From')}\nSubject: {headers.get('Subject')}\nDate: {headers.get('Date')}\nSnippet: {snippet}\n---")
            
        return "\n".join(output)
    except Exception as e:
        return f"Error searching emails: {format_api_error(e)}"

@mcp.tool()
def gmail_read_thread(thread_id: str) -> str:
    """
    Read the full content of an email thread. 
    Use this to get the body of emails found via search.
    """
    try:
        service = get_service()
        thread = service.users().threads().get(userId='me', id=thread_id).execute()
        messages = thread.get('messages', [])
        
        output = [f"Thread ID: {thread_id}"]
        
        for msg in messages:
            headers = {h['name']: h['value'] for h in msg['payload']['headers']}
            snippet = msg.get('snippet', '')
            
            # Simple body extraction logic
            body = snippet # Fallback
            if 'parts' in msg['payload']:
                for part in msg['payload']['parts']:
                    if part['mimeType'] == 'text/plain':
                        data = part['body'].get('data')
                        if data:
                            body = base64.urlsafe_b64decode(data).decode('utf-8')
            elif 'body' in msg['payload']:
                data = msg['payload']['body'].get('data')
                if data:
                    body = base64.urlsafe_b64decode(data).decode('utf-8')

            output.append(f"\n--- Message ID: {msg['id']} ---")
            output.append(f"From: {headers.get('From')}")
            output.append(f"Date: {headers.get('Date')}")
            output.append(f"Body:\n{body}\n")
            
        return "\n".join(output)
    except Exception as e:
        return f"Error reading thread: {format_api_error(e)}"

@mcp.tool()
def gmail_create_draft(to: str, subject: str, body: str) -> str:
    """
    Create a draft email. 
    Allows you to prepare an email for the user to review and send manually.
    """
    try:
        service = get_service()
        
        message = EmailMessage()
        message.set_content(body)
        message['To'] = to
        message['Subject'] = subject

        encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
        create_message = {'message': {'raw': encoded_message}}
        
        draft = service.users().drafts().create(userId='me', body=create_message).execute()
        return f"[SUCCESS] Draft created! Draft ID: {draft['id']}"
    except Exception as e:
        return f"Error creating draft: {format_api_error(e)}"

@mcp.tool()
def gmail_batch_modify_messages(message_ids: List[str], add_label_ids: List[str] = [], remove_label_ids: List[str] = []) -> str:
    """
    Modify labels for multiple messages at once.
    Use this to:
    - Trash messages: add_label_ids=['TRASH']
    - Archive messages: remove_label_ids=['INBOX']
    - Star messages: add_label_ids=['STARRED']
    - Apply custom labels: add_label_ids=['Label_ID']
    """
    try:
        service = get_service()
        body = {
            'ids': message_ids,
            'addLabelIds': add_label_ids,
            'removeLabelIds': remove_label_ids
        }
        service.users().messages().batchModify(userId='me', body=body).execute()
        return f"Successfully modified {len(message_ids)} messages."
    except Exception as e:
        return f"Error modifying messages: {format_api_error(e)}"

if __name__ == "__main__":
    mcp.run()
