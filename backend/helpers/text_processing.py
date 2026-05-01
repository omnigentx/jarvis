"""Text processing helpers extracted from server.py."""
import re


def process_agent_response(response):
    """Helper to extract clean text from agent response."""
    text = ""
    if isinstance(response, str):
        text = response
    elif hasattr(response, 'output'):
        text = str(response.output)
    else:
        text = str(response)
    
    # Remove tool_outputs blocks
    text = re.sub(r"```tool_outputs.*?```", "", text, flags=re.DOTALL)
    # Remove <think>...</think> and <thinking>...</thinking> blocks (LLM reasoning traces)
    text = re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", "", text, flags=re.DOTALL)
    text = text.strip()
    return text


def clean_text_for_tts(text: str) -> str:
    """Strip URLs, links, and markdown formatting so TTS doesn't read the
    syntax aloud. Agents may freely emit markdown (headings, bullets, code
    fences, mermaid blocks) for the dashboard renderer; this function is
    the single point that flattens it for the audio pipeline.
    """
    if not text: return ""
    text = re.sub(r"```[\s\S]*?```", "", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"!\[.*?\]\(.*?\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"www\.\S+", "", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*([^*\n]+)\*\*", r"\1", text)
    text = re.sub(r"(?<![*\w])\*([^*\n]+)\*(?!\*)", r"\1", text)
    text = re.sub(r"(?<![_\w])_([^_\n]+)_(?!_)", r"\1", text)
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*>\s?", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s+", " ", text).strip()
    return text
