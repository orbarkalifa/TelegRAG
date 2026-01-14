import re


def escape_markdown_v2(text: str, entity_type: str = "text") -> str:
    """
    Escapes characters for Telegram MarkdownV2.
    Different escaping rules apply inside code blocks vs normal text.
    """
    if entity_type == "text":
        # Characters that MUST be escaped in normal text
        escape_chars = r'_*[]()~`>#+-=|{}.!'
    else:
        # Inside code blocks/inline code, only backslash and backtick need escaping
        escape_chars = r'\`'

    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)


def format_markdownv2_minimal(text: str) -> str:
    """
    A robust formatter that:
    1. Protects code blocks (```...```)
    2. Protects inline code (`...`)
    3. Protects bold (**...**) and italic (*...*)
    4. Escapes everything else for MarkdownV2
    """
    # 1. Protect code blocks
    code_blocks = []

    def save_code_block(match):
        code_blocks.append(match.group(0))
        return f"__CODE_BLOCK_{len(code_blocks) - 1}__"

    # We use a non-greedy match for code blocks
    text = re.sub(r'```.*?```', save_code_block, text, flags=re.DOTALL)

    # 2. Protect inline code
    inline_codes = []

    def save_inline_code(match):
        inline_codes.append(match.group(0))
        return f"__INLINE_CODE_{len(inline_codes) - 1}__"

    text = re.sub(r'`[^`\n]+`', save_inline_code, text)

    # 3. Handle formatting tokens and escape prose
    # Characters to protect: ** (bold), * (italic), • (bullet)
    # We'll split the text by bold and italic markers
    parts = re.split(r'(\*\*|\*)', text)

    processed_parts = []
    in_bold = False
    in_italic = False

    for part in parts:
        if part == "**":
            processed_parts.append(part)
            in_bold = not in_bold
        elif part == "*":
            processed_parts.append(part)
            in_italic = not in_italic
        else:
            # This is normal prose - escape it!
            # Also convert common bullet points to the '•' character
            escaped = part.replace("- ", "• ").replace("* ", "• ")
            processed_parts.append(escape_markdown_v2(escaped))

    text = "".join(processed_parts)

    # 4. Restore code entities
    # Note: Inside these, we only escape \ and `
    for i, block in enumerate(code_blocks):
        # Extract content, escape internals, wrap in backticks
        content = block[3:-3]
        safe_block = f"```\n{escape_markdown_v2(content, 'code')}\n```"
        text = text.replace(f"__CODE_BLOCK_{i}__", safe_block)

    for i, code in enumerate(inline_codes):
        content = code[1:-1]
        safe_code = f"`{escape_markdown_v2(content, 'code')}`"
        text = text.replace(f"__INLINE_CODE_{i}__", safe_code)

    return text