import os
import google.generativeai as genai
from typing import List
from app.models import Message


def generate_rag_response(user_query: str, context: str, history: List[Message]) -> str:
    genai.configure(api_key=os.getenv("GOOGLE_API_KEY"))
    model = genai.GenerativeModel("gemini-1.5-flash")

    # Define strict formatting rules for Telegram MarkdownV2 compatibility
    system_prompt = """
    You are a helpful AI assistant. Answer the user's question based ONLY on the provided context.
    If the context doesn't contain the answer, say "I don't have enough information in my documents to answer that."

    STRICT FORMATTING RULES:
    1. Use **bold** for headers or emphasis.
    2. Use bullet lists using the '•' character (Option+8 or U+2022) exclusively.
    3. Use backticks for `inline code` and triple backticks for ```fenced code blocks```.
    4. Do NOT use markdown links like [text](url). Instead, provide the raw URL on its own line.
    5. Do NOT use tables or nested lists.
    6. Ensure all math notation is simplified (no complex LaTeX if possible).
    """

    messages = [{"role": "user", "parts": [system_prompt]}]

    # Add history
    for msg in history:
        messages.append({"role": "model" if msg.role == "assistant" else "user", "parts": [msg.content]})

    # Final prompt with context
    final_user_input = f"Context:\n{context}\n\nQuestion: {user_query}"
    messages.append({"role": "user", "parts": [final_user_input]})

    response = model.generate_content(messages)
    return response.text