import os
import io
import uuid
import requests
import structlog
from pypdf import PdfReader
from celery import Celery
from celery.signals import worker_process_init
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


@worker_process_init.connect
def init_worker(**kwargs):
    """
    Pre-loads the embedding model when the worker process starts.
    This prevents the 'stuck' behavior caused by lazy loading in tasks.
    """
    global _embedding_model
    log.info("worker_init_loading_model")
    # Initializing here ensures the model is ready in the fork-pool memory
    _embedding_model = TextEmbedding()
    log.info("worker_init_model_ready")


def get_embedding_model():
    global _embedding_model
    if _embedding_model is None:
        _embedding_model = TextEmbedding()
    return _embedding_model


def send_telegram(chat_id: int, text: str, parse_mode: str = "HTML"):
    try:
        if len(text) > 4000:
            text = text[:4000] + "...\n\n(Truncated)"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
        resp = requests.post(TELEGRAM_SEND_MESSAGE_URL, json=payload, timeout=10)
        if resp.status_code == 400:
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
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start += (chunk_size - overlap)
    return chunks


def extract_text_from_file(file_bytes: bytes, filename: str) -> Optional[str]:
    filename = filename.lower()
    try:
        if filename.endswith(".pdf"):
            reader = PdfReader(io.BytesIO(file_bytes))
            text = "\n".join([p.extract_text() for p in reader.pages if p.extract_text()])
            return text if text.strip() else None

        if b'\x00' in file_bytes[:1024]:
            return None

        decoded_text = file_bytes.decode("utf-8", errors="ignore")
        return decoded_text if decoded_text.strip() else None
    except Exception as e:
        log.error("extraction_failed", error=str(e), filename=filename)
        return None


@celery_app.task(name="app.worker.process_rag_message")
def process_rag_message(chat_id: int, text: str, trace_id: str):
    structlog.contextvars.bind_contextvars(trace_id=trace_id)
    send_typing(chat_id)

    with Session(sync_engine) as db:
        try:
            client = QdrantClient(host="qdrant", port=6333)
            model = get_embedding_model()

            # Use generator to handle memory better
            query_vector = next(model.embed([text]))

            search_res = []
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
            except Exception as qe:
                log.warning("qdrant_search_failed", error=str(qe))

            context_text = ""
            for hit in search_res:
                source = hit.payload.get('source', 'Unknown')
                chunk = hit.payload.get('text', '')
                context_text += f"[Source: {source}]\n{chunk}\n\n"

            history_query = select(Message).where(Message.user_id == chat_id).order_by(desc(Message.created_at)).limit(
                6)
            history = list(reversed(db.exec(history_query).all()))

            reply_text = generate_rag_response(text, context_text or "No private context found.", history)

            db.add(Message(user_id=chat_id, role="user", content=text, trace_id=trace_id))
            db.add(Message(user_id=chat_id, role="assistant", content=reply_text, trace_id=trace_id))
            db.commit()

            send_telegram(chat_id, reply_text)
        except Exception as e:
            log.error("rag_task_failed", error=str(e))
            send_telegram(chat_id, "⚠️ Sorry, I encountered an error processing that request.")


@celery_app.task(name="app.worker.process_document_upload")
def process_document_upload(chat_id: int, file_id: str, file_name: str, trace_id: str):
    structlog.contextvars.bind_contextvars(trace_id=trace_id)
    log.info("upload_started", file_name=file_name)

    try:
        # 1. Download from Telegram
        get_path_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile?file_id={file_id}"
        file_path = requests.get(get_path_url, timeout=10).json()["result"]["file_path"]
        file_bytes = requests.get(f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}",
                                  timeout=30).content
        log.info("file_downloaded", size=len(file_bytes))

        # 2. Extract Text
        full_text = extract_text_from_file(file_bytes, file_name)
        if full_text is None:
            send_telegram(chat_id, f"❌ <b>{file_name}</b> is not a readable text file.")
            return

        # 3. Chunking
        chunks = chunk_text(full_text)
        log.info("text_chunked", count=len(chunks))

        # 4. Embedding
        model = get_embedding_model()
        embeddings = list(model.embed(chunks))
        log.info("embeddings_generated")

        # 5. Qdrant Upsert
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
                payload={"text": c, "source": file_name, "user_id": chat_id}
            ) for c, v in zip(chunks, embeddings)
        ]

        client.upsert(collection_name="knowledge_base", points=points)
        log.info("upsert_complete", point_count=len(points))

        # 6. Database Record
        with Session(sync_engine) as db:
            db.add(Upload(filename=file_name, user_id=chat_id, qdrant_collection="knowledge_base",
                          chunk_count=len(chunks)))
            db.commit()

        send_telegram(chat_id, f"✅ <b>{file_name}</b> indexed successfully.")

    except Exception as e:
        log.error("upload_task_failed", error=str(e))
        send_telegram(chat_id, f"❌ Error processing <b>{file_name}</b>.")


@celery_app.task(name="app.worker.process_command")
def process_command(chat_id: int, command: str, trace_id: str):
    structlog.contextvars.bind_contextvars(trace_id=trace_id)
    client = QdrantClient(host="qdrant", port=6333)

    if command == "/start":
        msg = "👋 <b>RAG Bot Active</b>\nUpload PDFs or text files to start."
    elif command == "/newchat":
        with Session(sync_engine) as db:
            db.exec(delete(Message).where(Message.user_id == chat_id))
            db.commit()
        msg = "🧹 <b>Chat history cleared.</b>"
    elif command == "/reset":
        try:
            client.delete(collection_name="knowledge_base", points_selector=Filter(
                must=[FieldCondition(key="user_id", match=MatchValue(value=chat_id))]
            ))
            with Session(sync_engine) as db:
                db.exec(delete(Upload).where(Upload.user_id == chat_id))
                db.exec(delete(Message).where(Message.user_id == chat_id))
                db.commit()
            msg = "💥 <b>Knowledge base wiped.</b>"
        except Exception as e:
            log.error("reset_failed", error=str(e))
            msg = "⚠️ <b>Reset failed.</b>"
    else:
        msg = "Unknown command."

    send_telegram(chat_id, msg)