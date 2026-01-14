import os
from google import genai
from typing import List
from structlog import get_logger

log = get_logger()

api_key = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=api_key) if api_key else None
MODEL_ID = os.getenv('GEMINI_MODEL_ID', 'gemini-2.0-flash-exp')


def generate_rag_response(question: str, context_text: str, history: List[dict]) -> str:
    """
    Constructs the prompt and calls Gemini, explicitly requesting Markdown.
    """
    if not client:
        return "System Error: AI Client not initialized."

    history_str = ""
    for msg in history:
        role = "User" if msg.role == "user" else "Assistant"
        history_str += f"{role}: {msg.content}\n"

    prompt = f"""
    You are a professional Private RAG Assistant. 

    CORE RULES:
    1. PRIVACY: The "PRIVATE CONTEXT" below belongs ONLY to the current user. 
    2. FORMATTING: Use **Markdown** ONLY. Use *bold* for emphasis and bullet points for lists. 
    3. NO HTML: Never use tags like <ul>, <li>, or <b>.
    4. ACCURACY: If the answer is not in the context, say: "I don't have information about that in your documents."

    --- CONVERSATION HISTORY ---
    {history_str}

    --- PRIVATE CONTEXT ---
    {context_text if context_text.strip() else "No documents uploaded yet."}

    User Question: {question}
    """

    try:
        response = client.models.generate_content(
            model=MODEL_ID,
            contents=prompt
        )
        return response.text.strip()

    except Exception as e:
        log.error("gemini_generation_failed", error=str(e))
        return "I'm having trouble thinking. Please try again."