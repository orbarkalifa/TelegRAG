import re

# Telegram MarkdownV2 special chars (outside code/pre)
_MD2_SPECIALS = r"_*[]()~`>#+-=|{}.!\\"

def _escape_md2_text(s: str) -> str:
    # Escape every special char for MarkdownV2 (outside code)
    return re.sub(rf"([{re.escape(_MD2_SPECIALS)}])", r"\\\1", s)

def _escape_md2_code(s: str) -> str:
    # Inside `code` and ```pre``` only `\` and backtick must be escaped
    return s.replace("\\", "\\\\").replace("`", "\\`")

_CODEBLOCK_RE = re.compile(r"```(\w+)?\n([\s\S]*?)```", re.MULTILINE)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")

# Very conservative bold: only **...** on the same line (no nesting)
_BOLD_RE = re.compile(r"\*\*([^\n*][\s\S]*?[^\n*])\*\*")

def telegram_markdownv2_sanitize(text: str) -> str:
    if not text:
        return ""

    # Normalize newlines
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # 1) Protect fenced code blocks
    codeblocks = []
    def _cb_sub(m):
        lang = m.group(1) or ""
        body = m.group(2) or ""
        codeblocks.append((lang, body))
        return f"__CB_{len(codeblocks)-1}__"
    text = _CODEBLOCK_RE.sub(_cb_sub, text)

    # 2) Protect inline code
    inlines = []
    def _ic_sub(m):
        body = m.group(1) or ""
        inlines.append(body)
        return f"__IC_{len(inlines)-1}__"
    text = _INLINE_CODE_RE.sub(_ic_sub, text)

    # 3) Convert common LLM bullets BEFORE escaping
    # (avoid turning "* " into a formatting token)
    text = re.sub(r"(?m)^\s*-\s+", "• ", text)
    text = re.sub(r"(?m)^\s*\*\s+", "• ", text)

    # 4) Extract safe bold segments from LLM-style **...**
    bolds = []
    def _b_sub(m):
        bolds.append(m.group(1))
        return f"__B_{len(bolds)-1}__"
    text = _BOLD_RE.sub(_b_sub, text)

    # 5) Escape everything else as plain text
    text = _escape_md2_text(text)

    # 6) Restore bold segments as Telegram *...*
    for i, b in enumerate(bolds):
        safe_b = _escape_md2_text(b)  # still must escape specials inside bold
        text = text.replace(f"__B_{i}__", f"*{safe_b}*")

    # 7) Restore inline code
    for i, body in enumerate(inlines):
        safe_body = _escape_md2_code(body)
        text = text.replace(f"__IC_{i}__", f"`{safe_body}`")

    # 8) Restore fenced code blocks
    for i, (lang, body) in enumerate(codeblocks):
        safe_body = _escape_md2_code(body)
        if lang:
            text = text.replace(f"__CB_{i}__", f"```{lang}\n{safe_body}```")
        else:
            text = text.replace(f"__CB_{i}__", f"```\n{safe_body}```")

    return text
