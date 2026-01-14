import os
import uuid
from fastapi import FastAPI, Request, Header, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field
from typing import Optional
import structlog

from app.worker import celery_app

# Structured Logging Setup
structlog.configure(
    processors=[
        structlog.contextvars.inject_contextvars,
        structlog.processors.JSONRenderer()
    ]
)
log = structlog.get_logger()

app = FastAPI(title="TeleRAG Bot API")

TELEGRAM_SECRET_TOKEN = os.getenv("TELEGRAM_SECRET_TOKEN")


class TelegramMessage(BaseModel):
    message_id: int
    chat: dict
    text: Optional[str] = None
    document: Optional[dict] = None
    photo: Optional[list] = None


class TelegramUpdate(BaseModel):
    update_id: int
    message: Optional[TelegramMessage] = None


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.post("/webhook")
async def telegram_webhook(
        update: TelegramUpdate,
        x_telegram_bot_api_secret_token: Optional[str] = Header(None)
):
    """
    Handles incoming webhooks from Telegram.
    Routes messages, documents, and commands to the background worker.
    """
    # 1. Security Check
    if TELEGRAM_SECRET_TOKEN and x_telegram_bot_api_secret_token != TELEGRAM_SECRET_TOKEN:
        log.warning("unauthorized_webhook_attempt")
        raise HTTPException(status_code=403, detail="Unauthorized")

    if not update.message:
        return {"status": "ignored_no_message"}

    chat_id = update.message.chat.get("id")
    trace_id = str(uuid.uuid4())

    log.info("webhook_received", chat_id=chat_id, trace_id=trace_id)

    # 2. Route by Content Type

    # Case A: User sent a command
    if update.message.text and update.message.text.startswith("/"):
        celery_app.send_task(
            "app.worker.process_command",
            args=[chat_id, update.message.text, trace_id]
        )

    # Case B: User sent a document (PDF/Text)
    elif update.message.document:
        doc = update.message.document
        celery_app.send_task(
            "app.worker.process_document_upload",
            args=[chat_id, doc["file_id"], doc.get("file_name", "document"), trace_id]
        )

    # Case C: User sent a text message (RAG Query)
    elif update.message.text:
        celery_app.send_task(
            "app.worker.process_rag_message",
            args=[chat_id, update.message.text, trace_id]
        )

    return {"status": "queued", "trace_id": trace_id}