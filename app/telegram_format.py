# telegram_format.py
import re

# Telegram MarkdownV2 special chars
_MD2_SPECIALS = r"_*[]()~`>#+-=|{}.!\\"

def escape_md2(s: str) -> str:
    """Escape text for Telegram MarkdownV2 (outside code)."""
    return re.sub(rf"([{re.escape(_MD2_SPECIALS)}])", r"\\\1", s)

def escape_code(s: str) -> str:
    """Escape inside `code` and ```code``` blocks (only backslash and backtick)."""
    return s.replace("\\", "\\\\").replace("`", "\\`")

# Regexes
_CODEBLOCK_RE = re.compile(r"```(\w+)?\n([\s\S]*?)```", re.MULTILINE)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*([^\n*][\s\S]*?[^\n*])\*\*")  # conservative

def telegram_md2(text: str) -> str:
    """
    Convert a small subset of Markdown-like output into safe Telegram MarkdownV2:
    - **bold** -> *bold*
    - `inline` preserved
    - ```fenced``` preserved
    - bullets -, * -> •
    Everything else is escaped.
    """
    if not text:
        return ""

    t = text.replace("\r\n", "\n").replace("\r", "\n")

    # Protect blocks with tokens that contain NO md2 specials (no _,*,[,],..., etc.)
    codeblocks: list[tuple[str, str]] = []
    inlines: list[str] = []
    bolds: list[str] = []

    def cb_sub(m):
        idx = len(codeblocks)
        codeblocks.append((m.group(1) or "", m.group(2) or ""))
        return f"CBTOKEN{idx}X"

    def ic_sub(m):
        idx = len(inlines)
        inlines.append(m.group(1) or "")
        return f"ICTOKEN{idx}X"

    def b_sub(m):
        idx = len(bolds)
        bolds.append(m.group(1))
        return f"BOLDTOKEN{idx}X"

    t = _CODEBLOCK_RE.sub(cb_sub, t)
    t = _INLINE_CODE_RE.sub(ic_sub, t)

    # Normalize bullets before escaping
    t = re.sub(r"(?m)^\s*-\s+", "• ", t)
    t = re.sub(r"(?m)^\s*\*\s+", "• ", t)

    t = _BOLD_RE.sub(b_sub, t)

    # Escape all remaining text
    t = escape_md2(t)

    # Restore bold
    for i, b in enumerate(bolds):
        t = t.replace(f"BOLDTOKEN{i}X", f"*{escape_md2(b)}*")

    # Restore inline code
    for i, body in enumerate(inlines):
        t = t.replace(f"ICTOKEN{i}X", f"`{escape_code(body)}`")

    # Restore code blocks
    for i, (lang, body) in enumerate(codeblocks):
        body = escape_code(body)
        if lang:
            t = t.replace(f"CBTOKEN{i}X", f"```{lang}\n{body}```")
        else:
            t = t.replace(f"CBTOKEN{i}X", f"```\n{body}```")

    return t
