import os
import io
import requests
import structlog
from pypdf import PdfReader
from celery import Celery
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct
from fastembed import TextEmbedding
from sqlmodel import Session, select, desc, delete
import uuid
from app.database import sync_engine
from app.models import User, Message, Upload
from app.rag_engine import generate_rag_response

log = structlog.get_logger()

celery_app = Celery(
    "worker",
    broker=os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0"),
    backend=os.getenv("CELERY_RESULT_BACKEND", "redis://redis:6379/0")
)

_embedding_model = None
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_SEND_MESSAGE_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
TELEGRAM_CHAT_ACTION_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendChatAction"

def send_typing(chat_id: int):
    """Shows 'typing...' in the Telegram header."""
    try:
        requests.post(TELEGRAM_CHAT_ACTION_URL, json={
            "chat_id": chat_id,
            "action": "typing"
        })
    except Exception as e:
        log.warning("send_typing_failed", error=str(e))

def get_embedding_model():
    global _embedding_model
    if _embedding_model is None:
        _embedding_model = TextEmbedding()
    return _embedding_model


# --- IMPROVED CHUNKER ---
def chunk_text(text: str, chunk_size: int = 1000, overlap: int = 200) -> list[str]:
    """
    Splits text with overlap to preserve context across boundaries.
    Increased chunk_size to 1000 for better semantic meaning.
    """
    if not text:
        return []

    chunks = []
    start = 0
    text_len = len(text)

    while start < text_len:
        end = start + chunk_size
        chunk = text[start:end]
        chunks.append(chunk)
        # Move forward, but back up by 'overlap' amount
        start += (chunk_size - overlap)

    return chunks


# --- HELPER: Extract Text ---
def extract_text_from_file(file_bytes: bytes, filename: str) -> str:
    filename = filename.lower()
    try:
        if filename.endswith(".pdf"):
            pdf_file = io.BytesIO(file_bytes)
            reader = PdfReader(pdf_file)
            text = ""
            for page in reader.pages:
                extracted = page.extract_text()
                if extracted:
                    text += extracted + "\n"
            return text
        else:
            # Try utf-8, fallback to latin-1 if it fails
            return file_bytes.decode("utf-8", errors="replace")
    except Exception as e:
        log.error("file_extraction_error", error=str(e))
        return ""


# --- TASK: RAG MESSAGE ---
@celery_app.task(name="app.worker.process_rag_message")
def process_rag_message(chat_id: int, text: str, trace_id: str):
    structlog.contextvars.bind_contextvars(trace_id=trace_id)
    log.info("worker_started", chat_id=chat_id)

    send_typing(chat_id)

    with Session(sync_engine) as db:
        try:
            # 1. User & Message Recording
            user = db.exec(select(User).where(User.user_id == chat_id)).first()
            if not user:
                user = User(user_id=chat_id)
                db.add(user)
                db.commit()

            db.add(Message(user_id=chat_id, role="user", content=text, trace_id=trace_id))
            db.commit()

            # 2. Retrieval
            client = QdrantClient(host="qdrant", port=6333)
            model = get_embedding_model()

            query_vector = list(model.embed([text]))[0]

            search_response = client.query_points(
                collection_name="knowledge_base",
                query=query_vector,
                limit=5,  # Get top 5
                with_payload=True
            )

            # --- DEBUG LOG: SEE WHAT WE FOUND ---
            points_found = len(search_response.points)
            log.info("qdrant_search_results", count=points_found)

            context_text = ""
            if points_found > 0:
                for hit in search_response.points:
                    source = hit.payload.get('source', 'Unknown')
                    chunk = hit.payload.get('text', '')
                    context_text += f"[Source: {source}]\n{chunk}\n\n"
            else:
                context_text = "No relevant documents found."

            # 3. Generation
            # Check if history exists
            history_query = select(Message).where(Message.user_id == chat_id).order_by(desc(Message.created_at)).limit(
                5)
            history = list(reversed(db.exec(history_query).all()))

            reply_text = generate_rag_response(text, context_text, history)

            # 4. Save & Send
            db.add(Message(user_id=chat_id, role="assistant", content=reply_text, trace_id=trace_id))
            db.commit()

            requests.post(TELEGRAM_SEND_MESSAGE_URL, json={"chat_id": chat_id, "text": reply_text, "parse_mode": "Markdown"})
            log.info("worker_success", chat_id=chat_id)

        except Exception as e:
            log.error("worker_failed", error=str(e))
            db.rollback()


# --- TASK: UPLOAD ---
@celery_app.task(name="app.worker.process_document_upload")
def process_document_upload(chat_id: int, file_id: str, file_name: str, trace_id: str):
    structlog.contextvars.bind_contextvars(trace_id=trace_id)
    log.info("worker_upload_started", filename=file_name)

    # Notify user processing started
    requests.post(TELEGRAM_SEND_MESSAGE_URL, json={"chat_id": chat_id, "text": f"⏳ Reading *{file_name}*..."})

    try:
        file_bytes = download_telegram_file(file_id)
        full_text = extract_text_from_file(file_bytes, file_name)

        if not full_text.strip():
            raise ValueError("File is empty or could not be read.")

        # Chunk with overlap
        text_chunks = chunk_text(full_text, chunk_size=1000, overlap=200)

        if not text_chunks:
            raise ValueError("Text extraction resulted in 0 chunks.")

        log.info("text_chunked", chunks=len(text_chunks))

        client = QdrantClient(host="qdrant", port=6333)
        model = get_embedding_model()
        embeddings = list(model.embed(text_chunks))

        points = []
        for i, (chunk, vector) in enumerate(zip(text_chunks, embeddings)):
            points.append(PointStruct(
                id=str(uuid.uuid4()),
                vector=vector,
                payload={"text": chunk, "source": file_name, "chunk_index": i}
            ))

        client.upsert(collection_name="knowledge_base", points=points)

        with Session(sync_engine) as db:
            db.add(Upload(filename=file_name, qdrant_collection="knowledge_base", chunk_count=len(text_chunks)))
            db.commit()

        requests.post(TELEGRAM_SEND_MESSAGE_URL, json={"chat_id": chat_id,
                                              "text": f"✅ Processed *{file_name}*. I read {len(text_chunks)} segments.",
                                              "parse_mode": "Markdown"})
        log.info("worker_upload_success", filename=file_name)

    except Exception as e:
        log.error("worker_upload_failed", error=str(e))
        requests.post(TELEGRAM_SEND_MESSAGE_URL, json={"chat_id": chat_id, "text": f"❌ Error: {str(e)}"})


# --- Helper: Download ---
def download_telegram_file(file_id: str) -> bytes:
    get_path_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile?file_id={file_id}"
    resp = requests.get(get_path_url).json()
    file_path = resp["result"]["file_path"]
    download_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
    return requests.get(download_url).content


@celery_app.task(name="app.worker.process_command")
def process_command(chat_id: int, command: str, trace_id: str):
    structlog.contextvars.bind_contextvars(trace_id=trace_id)
    log.info("worker_command_started", command=command)

    reply_text = ""

    try:
        # --- COMMAND: /START ---
        if command == "/start":
            reply_text = (
                "👋 **Welcome to your RAG Bot!**\n\n"
                "I can read files and answer questions about them.\n\n"
                "**How to use me:**\n"
                "1. Drag & drop a PDF, TXT, or MD file here.\n"
                "2. Wait for me to read it.\n"
                "3. Ask questions!\n\n"
                "**Commands:**\n"
                "/newchat - Clear conversation history\n"
                "/reset - Delete all knowledge (Files)\n"
                "/help - Show this menu"
            )

        # --- COMMAND: /HELP ---
        elif command == "/help":
            reply_text = (
                "**Available Commands:**\n\n"
                "🗑️ /newchat - Forgets our *current conversation* so I stop using old context.\n"
                "💥 /reset - Wipes my *Knowledge Base*. Deletes all uploaded files from my memory.\n"
                "ℹ️ /start - About me."
            )

        # --- COMMAND: /NEWCHAT (Clear Memory) ---
        elif command == "/newchat":
            with Session(sync_engine) as db:
                # Delete messages for this user
                statement = delete(Message).where(Message.user_id == chat_id)
                db.exec(statement)
                db.commit()

            reply_text = "🧹 **Context Cleared.**\nI have forgotten our previous conversation. We are starting fresh!"

        # --- COMMAND: /RESET (Clear Knowledge) ---
        elif command == "/reset" or command == "/reset-knowledge":
            # 1. Clear Qdrant
            client = QdrantClient(host="qdrant", port=6333)
            client.delete_collection("knowledge_base")
            # Recreate immediately so it's ready for new uploads
            client.create_collection(
                collection_name="knowledge_base",
                vectors_config={"size": 384, "distance": "Cosine"}
            )

            # 2. Clear Postgres Upload Logs
            with Session(sync_engine) as db:
                db.exec(delete(Upload))  # Delete all upload records
                db.commit()

            reply_text = "💥 **System Reset.**\nAll documents and knowledge have been wiped. I am a blank slate."

        # --- UNKNOWN COMMAND ---
        else:
            reply_text = f"I don't recognize the command `{command}`. Try /help."

        # Send Reply
        requests.post(TELEGRAM_SEND_MESSAGE_URL, json={"chat_id": chat_id, "text": reply_text, "parse_mode": "Markdown"})
        log.info("worker_command_success", command=command)

    except Exception as e:
        log.error("worker_command_failed", error=str(e))
        requests.post(TELEGRAM_SEND_MESSAGE_URL, json={"chat_id": chat_id, "text": "❌ Command failed to execute."})