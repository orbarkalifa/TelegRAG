import os
import io
import uuid
import requests
import structlog
from pypdf import PdfReader
from celery import Celery
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct, VectorParams, Distance, Filter, FieldCondition, MatchValue
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

# Global holder for the model
_embedding_model = None

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_SEND_MESSAGE_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
TELEGRAM_CHAT_ACTION_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendChatAction"


def get_embedding_model():
    """Lazily loads the embedding model."""
    global _embedding_model
    if _embedding_model is None:
        _embedding_model = TextEmbedding()
    return _embedding_model


def send_telegram(chat_id: int, text: str, parse_mode: str = "HTML"):
    """
    Sends messages with HTML mode.
    Includes a 400-error fallback to plain text if AI formatting is invalid.
    """
    try:
        if len(text) > 4000:
            text = text[:4000] + "...\n\n(Truncated)"

        payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
        resp = requests.post(TELEGRAM_SEND_MESSAGE_URL, json=payload, timeout=10)

        if resp.status_code == 400:
            log.warning("telegram_format_error_fallback", chat_id=chat_id, response=resp.text)
            payload.pop("parse_mode")
            resp = requests.post(TELEGRAM_SEND_MESSAGE_URL, json=payload, timeout=10)

        resp.raise_for_status()
    except Exception as e:
        log.error("telegram_send_failed", error=str(e), chat_id=chat_id)


def send_typing(chat_id: int):
    try:
        requests.post(TELEGRAM_CHAT_ACTION_URL, json={"chat_id": chat_id, "action": "typing"}, timeout=5)
    except Exception:
        pass


def chunk_text(text: str, chunk_size: int = 1000, overlap: int = 200) -> List[str]:
    if not text: return []
    if overlap >= chunk_size: overlap = chunk_size // 2
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start += (chunk_size - overlap)
    return chunks


def extract_text_from_file(file_bytes: bytes, filename: str) -> str:
    """
    Extracts text from PDF or attempts to decode as UTF-8 for any other file.
    Returns None if the file appears to be binary or unreadable.
    """
    filename = filename.lower()
    try:
        # Handle PDFs specifically
        if filename.endswith(".pdf"):
            reader = PdfReader(io.BytesIO(file_bytes))
            text = "\n".join([p.extract_text() for p in reader.pages if p.extract_text()])
            return text if text.strip() else None

        # Attempt to decode as text (handles .txt, .py, .js, .html, .md, .json, etc.)
        try:
            # Check for null bytes which usually indicate a binary file
            if b'\x00' in file_bytes[:1024]:
                log.warning("binary_file_detected", filename=filename)
                return None

            decoded_text = file_bytes.decode("utf-8")
            return decoded_text if decoded_text.strip() else None
        except UnicodeDecodeError:
            log.warning("file_not_utf8_readable", filename=filename)
            return None

    except Exception as e:
        log.error("extraction_failed", error=str(e), filename=filename)
        return None


@celery_app.task(name="app.worker.process_rag_message")
def process_rag_message(chat_id: int, text: str, trace_id: str):
    structlog.contextvars.bind_contextvars(trace_id=trace_id)
    send_typing(chat_id)

    with Session(sync_engine) as db:
        try:
            user = db.exec(select(User).where(User.user_id == chat_id)).first()
            if not user:
                user = User(user_id=chat_id)
                db.add(user)
            db.add(Message(user_id=chat_id, role="user", content=text, trace_id=trace_id))
            db.commit()

            client = QdrantClient(host="qdrant", port=6333)
            model = get_embedding_model()
            query_vector = list(model.embed([text]))[0]

            try:
                search_res = client.query_points(
                    collection_name="knowledge_base",
                    query=query_vector,
                    query_filter=Filter(
                        must=[FieldCondition(key="user_id", match=MatchValue(value=chat_id))]
                    ),
                    limit=5,
                    with_payload=True
                ).points
            except Exception:
                search_res = []

            context_text = ""
            for hit in search_res:
                source = hit.payload.get('source', 'Unknown')
                chunk = hit.payload.get('text', '')
                context_text += f"[Source: {source}]\n{chunk}\n\n"

            history_query = select(Message).where(Message.user_id == chat_id).order_by(desc(Message.created_at)).limit(
                6)
            history = list(reversed(db.exec(history_query).all()))

            reply_text = generate_rag_response(text, context_text or "No private context found.", history)
            db.add(Message(user_id=chat_id, role="assistant", content=reply_text, trace_id=trace_id))
            db.commit()

            send_telegram(chat_id, reply_text)

        except Exception as e:
            log.error("rag_task_failed", error=str(e))
            send_telegram(chat_id, "⚠️ Sorry, I encountered an error processing that request.")


@celery_app.task(name="app.worker.process_document_upload")
def process_document_upload(chat_id: int, file_id: str, file_name: str, trace_id: str):
    structlog.contextvars.bind_contextvars(trace_id=trace_id)
    send_telegram(chat_id, f"⏳ Analyzing <b>{file_name}</b>...")

    try:
        # Download
        get_path_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile?file_id={file_id}"
        file_path = requests.get(get_path_url, timeout=10).json()["result"]["file_path"]
        file_bytes = requests.get(f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}",
                                  timeout=30).content

        full_text = extract_text_from_file(file_bytes, file_name)

        # Validation for non-readable files
        if full_text is None:
            send_telegram(chat_id,
                          f"❌ <b>{file_name}</b> is a binary or non-readable file. I can only process text-based documents (PDF, Code, TXT, etc.).")
            return

        chunks = chunk_text(full_text)
        if not chunks:
            send_telegram(chat_id, f"⚠️ <b>{file_name}</b> seems to be empty or contains no extractable text.")
            return

        model = get_embedding_model()
        embeddings = list(model.embed(chunks))

        client = QdrantClient(host="qdrant", port=6333)

        if not client.collection_exists("knowledge_base"):
            client.create_collection(
                collection_name="knowledge_base",
                vectors_config=VectorParams(size=len(embeddings[0]), distance=Distance.COSINE)
            )

        points = [
            PointStruct(
                id=str(uuid.uuid4()),
                vector=v.tolist(),
                payload={
                    "text": c,
                    "source": file_name,
                    "user_id": chat_id,
                    "index": i
                }
            ) for i, (c, v) in enumerate(zip(chunks, embeddings))
        ]

        client.upsert(collection_name="knowledge_base", points=points)

        with Session(sync_engine) as db:
            db.add(Upload(filename=file_name, user_id=chat_id, qdrant_collection="knowledge_base",
                          chunk_count=len(chunks)))
            db.commit()

        send_telegram(chat_id, f"✅ <b>{file_name}</b> has been successfully indexed in your private knowledge base.")

    except Exception as e:
        log.error("upload_task_failed", error=str(e))
        send_telegram(chat_id, f"❌ Failed to process document: {str(e)}")


@celery_app.task(name="app.worker.process_command")
def process_command(chat_id: int, command: str, trace_id: str):
    structlog.contextvars.bind_contextvars(trace_id=trace_id)
    client = QdrantClient(host="qdrant", port=6333)

    if command == "/start":
        msg = "👋 <b>RAG Bot Active</b>\nUpload any text-based file (PDF, Code, TXT) and I will analyze it privately for you."
    elif command == "/newchat":
        with Session(sync_engine) as db:
            db.exec(delete(Message).where(Message.user_id == chat_id))
            db.commit()
        msg = "🧹 <b>Chat history cleared.</b>"
    elif command == "/reset":
        try:
            client.delete(
                collection_name="knowledge_base",
                points_selector=Filter(
                    must=[FieldCondition(key="user_id", match=MatchValue(value=chat_id))]
                )
            )
            with Session(sync_engine) as db:
                db.exec(delete(Upload).where(Upload.user_id == chat_id))
                db.exec(delete(Message).where(Message.user_id == chat_id))
                db.commit()
            msg = "💥 <b>Your private data has been wiped.</b>"
        except Exception as e:
            log.error("reset_failed", error=str(e))
            msg = "⚠️ <b>Reset failed.</b>"
    else:
        msg = "Unknown command."

    send_telegram(chat_id, msg)