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
    Generates a response using the Gemini API, strictly adhering to the
    provided private user context.
    """
    if not client:
        return "⚠️ AI Client not configured. Please check your GEMINI_API_KEY."

    # Format chat history for context awareness
    history_str = ""
    for msg in history:
        role = "User" if msg.role == "user" else "Assistant"
        history_str += f"{role}: {msg.content}\n"

    # Strict System Instructions for Privacy and Hallucination Control
    prompt = f"""
    You are a professional Private RAG Assistant. 

    CORE RULES:
    1. PRIVACY: The "PRIVATE CONTEXT" below belongs ONLY to the current user. Never reference other users or external documents not provided here.
    2. ACCURACY: Answer the user's question using the PROVIDED CONTEXT. 
    3. HONESTY: If the answer is not in the context, say: "I'm sorry, I don't have information about that in your uploaded documents." 
    4. TONE: Be helpful, concise, and professional.
    5. FORMATTING: Use basic HTML tags (<b>, <i>, <code>) for emphasis. Avoid complex Markdown that might break the Telegram parser.

    --- CONVERSATION HISTORY ---
    {history_str}

    --- PRIVATE CONTEXT (USER DOCUMENTS) ---
    {context_text if context_text.strip() else "No documents have been uploaded by this user yet."}

    USER QUESTION: {question}
    """

    try:
        response = client.models.generate_content(
            model=MODEL_ID,
            contents=prompt
        )
        return response.text.strip()
    except Exception as e:
        log.error("gemini_inference_failed", error=str(e))
        return "I'm sorry, I encountered an error while thinking. Please try again in a moment."