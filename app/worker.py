import os
import io
import uuid
import requests
import structlog
import re
from pypdf import PdfReader
from celery import Celery
from celery.signals import worker_process_init
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct, VectorParams, Distance, Filter, FieldCondition, MatchValue, FilterSelector
from fastembed import TextEmbedding
from sqlmodel import Session, select, desc, delete
from typing import List, Optional

from app.database import sync_engine
from app.models import User, Message, Upload
from app.rag_engine import generate_rag_response
from app.telegram_format import telegram_md2

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


@worker_process_init.connect
def init_worker(**kwargs):
    global _embedding_model
    log.info("worker_init_loading_model")
    _embedding_model = TextEmbedding()
    log.info("worker_init_model_ready")


def get_embedding_model():
    global _embedding_model
    if _embedding_model is None:
        _embedding_model = TextEmbedding()
    return _embedding_model


def send_telegram(chat_id: int, text: str):
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")

    # Format to MarkdownV2
    formatted = telegram_md2(raw)
    payload = {"chat_id": chat_id, "text": formatted, "parse_mode": "MarkdownV2"}

    # Telegram hard limit is 4096; keep a small margin
    if len(payload["text"]) > 4096:
        payload["text"] = payload["text"][:4093] + "..."

    resp = requests.post(TELEGRAM_SEND_MESSAGE_URL, json=payload, timeout=10)

    # Fallback: plain text, no parse_mode (omit parse_mode entirely)
    if not resp.ok:
        log.warning("telegram_send_failed_markdown_fallback", response=resp.text)
        fallback_text = raw
        if len(fallback_text) > 4096:
            fallback_text = fallback_text[:4093] + "..."
        resp = requests.post(
            TELEGRAM_SEND_MESSAGE_URL,
            json={"chat_id": chat_id, "text": fallback_text},
            timeout=10
        )

    resp.raise_for_status()



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
        return file_bytes.decode("utf-8", errors="ignore")
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
            query_vector = next(model.embed([text]))

            search_res = []
            try:
                search_res = client.query_points(
                    collection_name="knowledge_base",
                    query=query_vector,
                    # Crucial: Keep private data separation
                    query_filter=Filter(must=[FieldCondition(key="user_id", match=MatchValue(value=chat_id))]),
                    limit=5,
                    with_payload=True
                ).points
            except Exception as qe:
                log.warning("qdrant_search_failed", error=str(qe))

            context_text = ""
            for hit in search_res:
                context_text += f"[Source: {hit.payload.get('source')}]\n{hit.payload.get('text')}\n\n"

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
            send_telegram(chat_id, "⚠️ Error processing request.")


@celery_app.task(name="app.worker.process_document_upload")
def process_document_upload(chat_id: int, file_id: str, file_name: str, trace_id: str):
    structlog.contextvars.bind_contextvars(trace_id=trace_id)
    send_telegram(chat_id, f"⏳ Reading **{file_name}**...")
    try:
        get_path_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile?file_id={file_id}"
        file_path = requests.get(get_path_url).json()["result"]["file_path"]
        file_bytes = requests.get(f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}").content

        full_text = extract_text_from_file(file_bytes, file_name)
        if not full_text:
            send_telegram(chat_id, f"❌ {file_name} is unreadable.")
            return

        chunks = chunk_text(full_text)
        model = get_embedding_model()
        embeddings = list(model.embed(chunks))

        client = QdrantClient(host="qdrant", port=6333)
        if not client.collection_exists("knowledge_base"):
            client.create_collection(
                collection_name="knowledge_base",
                vectors_config=VectorParams(size=len(embeddings[0]), distance=Distance.COSINE)
            )

        points = [
            PointStruct(id=str(uuid.uuid4()), vector=v.tolist(),
                        payload={"text": c, "source": file_name, "user_id": chat_id})
            for c, v in zip(chunks, embeddings)
        ]
        client.upsert(collection_name="knowledge_base", points=points)

        with Session(sync_engine) as db:
            db.add(Upload(filename=file_name, user_id=chat_id, qdrant_collection="knowledge_base",
                          chunk_count=len(chunks)))
            db.commit()

        send_telegram(chat_id, f"✅ **{file_name}** indexed.")
    except Exception as e:
        log.error("upload_failed", error=str(e))
        send_telegram(chat_id, f"❌ Error processing {file_name}.")


@celery_app.task(name="app.worker.process_command")
def process_command(chat_id: int, command: str, trace_id: str):
    structlog.contextvars.bind_contextvars(trace_id=trace_id)
    client = QdrantClient(host="qdrant", port=6333)
    msg = ""

    if command == "/start":
        msg = "👋 RAG Bot Active. Upload files to start."
    elif command == "/newchat":
        with Session(sync_engine) as db:
            db.exec(delete(Message).where(Message.user_id == chat_id))
            db.commit()
        msg = "🧹 Chat history cleared."
    elif command == "/reset":
        client.delete(collection_name="knowledge_base", points_selector=FilterSelector(
            filter=Filter(must=[FieldCondition(key="user_id", match=MatchValue(value=chat_id))])
        ))
        with Session(sync_engine) as db:
            db.exec(delete(Upload).where(Upload.user_id == chat_id))
            db.exec(delete(Message).where(Message.user_id == chat_id))
            db.commit()
        msg = "💥 Knowledge base wiped."

    if msg: send_telegram(chat_id, msg)