import os
from google import genai
from typing import List
from structlog import get_logger

log = get_logger()

# 1. Initialize Client
api_key = os.getenv("GEMINI_API_KEY")
client = None

if not api_key:
    log.error("startup_error", error="GEMINI_API_KEY is missing!")
else:
    client = genai.Client(api_key=api_key)

# 2. Select Model
MODEL_ID = os.getenv('GEMINI_MODEL_ID', 'gemini-2.5-flash-lite')


def generate_rag_response(question: str, context_text: str, history: List[dict]) -> str:
    """
    Constructs the prompt and calls the new Google GenAI Client.
    """
    if not client:
        return "System Error: AI Client not initialized."

    # --- A. Format History ---
    history_str = ""
    if history:
        history_str = "--- PREVIOUS CONVERSATION ---\n"
        for msg in history:
            # We use getattr to safely handle SQLModel objects
            role = getattr(msg, 'role', 'user').capitalize()
            content = getattr(msg, 'content', '')
            history_str += f"{role}: {content}\n"
        history_str += "-----------------------------\n"

    # --- B. Build Prompt (UPDATED LOGIC) ---
    prompt = f"""
    You are a smart and helpful AI assistant.

    SOURCES OF INFORMATION:
    1. **Context**: Text retrieved from uploaded documents.
    2. **History**: The recent conversation between you and the user.

    INSTRUCTIONS:
    1. **Priority**: Always check **Context** first for factual answers.
    2. **Conversational Fallback**: If the answer is NOT in the Context, check the **History**.
    3. **Memory**: If the user provides information (e.g., "My name is Or"), acknowledge it and remember it for the next turn.
    4. **Small Talk**: If the user says "Hello", "Thanks", or "Correct", reply naturally without needing documents.
    5. **Strict Limit**: ONLY say "I don't have enough information in my documents" if the answer is missing from BOTH Context AND History.

    {history_str}

    --- CONTEXT ---
    {context_text}
    ---------------

    User Question: {question}
    Assistant's Response:
    """

    # --- C. Call New API ---
    try:
        response = client.models.generate_content(
            model=MODEL_ID,
            contents=prompt
        )
        return response.text.strip()

    except Exception as e:
        log.error("gemini_generation_failed", error=str(e))
        return "I'm having trouble thinking right now. Please try again."