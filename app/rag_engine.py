import os
import structlog
from google import genai
from google.genai import types

log = structlog.get_logger()

# Initialize the new Google GenAI Client
client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))


def generate_rag_response(query: str, context: str, history: list = None) -> str:
    """
    Generates a response using the new google.genai SDK.
    """
    try:
        # Construct the system instruction for RAG
        system_prompt = (
                "You are a helpful AI assistant. Use the provided context to answer the user's question. "
                "If the answer is not in the context, say: 'I don't have enough information in my documents to answer that.' "
                "Context: \n" + context
        )

        # Prepare the contents (including history if available)
        contents = []
        if history:
            for msg in history:
                role = "user" if msg.role == "user" else "model"
                contents.append(types.Content(role=role, parts=[types.Part.from_text(text=msg.content or "")]))

        # Add the current query
        contents.append(types.Content(role="user", parts=[types.Part.from_text(text=query)]))

        # Generate response using the new SDK syntax
        response = client.models.generate_content(
            model=os.getenv('GEMINI_MODEL_ID'),
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=0.3,
            )
        )

        return response.text

    except Exception as e:
        log.error("gemini_inference_failed", error=str(e))
        return "I encountered an error while processing your request."