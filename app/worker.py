import os
import io
import uuid
import requests
import structlog
from pypdf import PdfReader
from celery import Celery
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct, VectorParams, Distance
from fastembed import TextEmbedding
from sqlmodel import Session, select, desc, delete
from typing import List, Optional

from app.database import sync_engine
from app.models import User, Message, Upload
from app.rag_engine import generate_rag_response

log = structlog.get_logger()

celery_app = Celery(
    "worker",
    broker=os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0"),
    backend=os.getenv("CELERY_RESULT_BACKEND", "redis://redis:6379/0")
)

# Global holder for the model to avoid re-loading per task,
# but initialized lazily to handle Celery forking safely.
_embedding_model = None

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_SEND_MESSAGE_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
TELEGRAM_CHAT_ACTION_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendChatAction"


def get_embedding_model():
    """Lazily loads the embedding model."""
    global _embedding_model
    if _embedding_model is None:
        # Note: model_name defaults to "BAAI/bge-small-en-v1.5" (384 dims)
        _embedding_model = TextEmbedding()
    return _embedding_model


def send_telegram(chat_id: int, text: str, parse_mode: str = "Markdown"):
    """Helper to send messages with basic error handling and length clipping."""
    try:
        # Telegram limit is 4096 chars
        if len(text) > 4000:
            text = text[:4000] + "...\n\n(Truncated due to length)"

        resp = requests.post(
            TELEGRAM_SEND_MESSAGE_URL,
            json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
            timeout=10
        )
        resp.raise_for_status()
    except Exception as e:
        log.error("telegram_send_failed", error=str(e), chat_id=chat_id)


def send_typing(chat_id: int):
    try:
        requests.post(TELEGRAM_CHAT_ACTION_URL, json={"chat_id": chat_id, "action": "typing"}, timeout=5)
    except Exception:
        pass


def chunk_text(text: str, chunk_size: int = 1000, overlap: int = 200) -> List[str]:
    if not text:
        return []

    # SAFETY CHECK: Prevent infinite loops
    if overlap >= chunk_size:
        overlap = chunk_size // 2
        log.warning("invalid_overlap_adjusted", new_overlap=overlap)

    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start += (chunk_size - overlap)
    return chunks


def extract_text_from_file(file_bytes: bytes, filename: str) -> str:
    filename = filename.lower()
    try:
        if filename.endswith(".pdf"):
            reader = PdfReader(io.BytesIO(file_bytes))
            return "\n".join([p.extract_text() for p in reader.pages if p.extract_text()])
        return file_bytes.decode("utf-8", errors="replace")
    except Exception as e:
        log.error("extraction_failed", error=str(e))
        return ""


@celery_app.task(name="app.worker.process_rag_message")
def process_rag_message(chat_id: int, text: str, trace_id: str):
    structlog.contextvars.bind_contextvars(trace_id=trace_id)
    send_typing(chat_id)

    with Session(sync_engine) as db:
        try:
            # 1. Ensure User exists and Log Message
            user = db.exec(select(User).where(User.user_id == chat_id)).first()
            if not user:
                user = User(user_id=chat_id)
                db.add(user)

            db.add(Message(user_id=chat_id, role="user", content=text, trace_id=trace_id))
            db.commit()

            # 2. Vector Search
            client = QdrantClient(host="qdrant", port=6333)
            model = get_embedding_model()
            query_vector = list(model.embed([text]))[0]

            try:
                search_res = client.query_points(
                    collection_name="knowledge_base",
                    query=query_vector,
                    limit=5,
                    with_payload=True
                ).points
            except Exception:
                log.warning("qdrant_collection_missing_or_failed")
                search_res = []

            context_text = ""
            for hit in search_res:
                source = hit.payload.get('source', 'Unknown')
                chunk = hit.payload.get('text', '')
                context_text += f"[Source: {source}]\n{chunk}\n\n"

            # 3. History & Generation
            history_query = select(Message).where(Message.user_id == chat_id).order_by(desc(Message.created_at)).limit(
                6)
            history = list(reversed(db.exec(history_query).all()))

            reply_text = generate_rag_response(text, context_text or "No relevant context found.", history)

            # 4. Save & Send
            db.add(Message(user_id=chat_id, role="assistant", content=reply_text, trace_id=trace_id))
            db.commit()

            send_telegram(chat_id, reply_text)

        except Exception as e:
            log.error("rag_task_failed", error=str(e))
            send_telegram(chat_id, "⚠️ Sorry, I encountered an error processing that request.")


@celery_app.task(name="app.worker.process_document_upload")
def process_document_upload(chat_id: int, file_id: str, file_name: str, trace_id: str):
    structlog.contextvars.bind_contextvars(trace_id=trace_id)
    send_telegram(chat_id, f"⏳ Reading *{file_name}*...")

    try:
        # Download and Extract
        get_path_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile?file_id={file_id}"
        file_path = requests.get(get_path_url, timeout=10).json()["result"]["file_path"]
        file_bytes = requests.get(f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}",
                                  timeout=30).content

        full_text = extract_text_from_file(file_bytes, file_name)
        if not full_text.strip():
            raise ValueError("Document appears to be empty.")

        # Chunking & Embedding
        chunks = chunk_text(full_text)
        model = get_embedding_model()
        embeddings = list(model.embed(chunks))

        # Vector DB Upsert
        client = QdrantClient(host="qdrant", port=6333)

        # Check if collection exists, if not, create it
        if not client.collection_exists("knowledge_base"):
            vector_size = len(embeddings[0])
            client.create_collection(
                collection_name="knowledge_base",
                vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE)
            )

        points = [
            PointStruct(
                id=str(uuid.uuid4()),
                vector=v.tolist(),
                payload={"text": c, "source": file_name, "index": i}
            ) for i, (c, v) in enumerate(zip(chunks, embeddings))
        ]

        client.upsert(collection_name="knowledge_base", points=points)

        with Session(sync_engine) as db:
            db.add(Upload(filename=file_name, qdrant_collection="knowledge_base", chunk_count=len(chunks)))
            db.commit()

        send_telegram(chat_id, f"✅ Processed *{file_name}* ({len(chunks)} segments).")

    except Exception as e:
        log.error("upload_task_failed", error=str(e))
        send_telegram(chat_id, f"❌ Failed to process document: {str(e)}")


@celery_app.task(name="app.worker.process_command")
def process_command(chat_id: int, command: str, trace_id: str):
    structlog.contextvars.bind_contextvars(trace_id=trace_id)
    client = QdrantClient(host="qdrant", port=6333)

    if command == "/start":
        msg = "👋 **RAG Bot Active**\nUpload documents and ask me anything!"
    elif command == "/newchat":
        with Session(sync_engine) as db:
            db.exec(delete(Message).where(Message.user_id == chat_id))
            db.commit()
        msg = "🧹 **Conversation history cleared.**"
    elif command == "/reset":
        client.delete_collection("knowledge_base")
        with Session(sync_engine) as db:
            db.exec(delete(Upload))
            db.commit()
        msg = "💥 **Knowledge base wiped.**"
    else:
        msg = "Unknown command. Try /start."

    send_telegram(chat_id, msg)