import uuid
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
import structlog
from app.worker import process_rag_message, process_document_upload, process_command
from app.database import init_db
from app.models import User, Message

log = structlog.get_logger()

# Use lifespan context manager for startup/shutdown events
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("startup_event", status="initializing_database")
    # This creates the tables in Postgres
    await init_db()
    log.info("startup_event", status="database_ready")
    yield

app = FastAPI(lifespan=lifespan)

# 1. Middleware: Assign Trace ID to every request
@app.middleware("http")
async def add_trace_id(request: Request, call_next):
    trace_id = str(uuid.uuid4())
    structlog.contextvars.bind_contextvars(trace_id=trace_id)

    response = await call_next(request)
    return response


@app.get("/")
async def health_check():
    return {"status": "ok", "service": "telegram-rag-bot"}


# 2. The Webhook Endpoint
@app.post("/webhook")
async def telegram_webhook(update: dict, request: Request):
    ctx = structlog.contextvars.get_contextvars()
    trace_id = ctx.get("trace_id")

    if "message" in update:
        msg = update["message"]
        chat_id = msg["chat"]["id"]

        # CASE 1: Text Message
        if "text" in msg:
            text = msg["text"]

            # CHECK FOR COMMANDS
            if text.startswith("/"):
                command = text.split(" ")[0].lower()  # Get "/start" from "/start now"
                log.info("webhook_command_received", chat_id=chat_id, command=command)
                process_command.delay(chat_id, command, trace_id)

            # NORMAL CHAT
            else:
                log.info("webhook_text_received", chat_id=chat_id)
                process_rag_message.delay(chat_id, text, trace_id)

        # CASE 2: Document Upload
        elif "document" in msg:
            doc = msg["document"]
            file_id = doc["file_id"]
            file_name = doc.get("file_name", "unknown.txt")

            allowed_exts = (".pdf", ".txt", ".md", ".json", ".csv", ".py")
            if file_name.lower().endswith(allowed_exts):
                log.info("webhook_doc_received", chat_id=chat_id, filename=file_name)
                process_document_upload.delay(chat_id, file_id, file_name, trace_id)
            else:
                log.warning("webhook_doc_rejected", filename=file_name)

    return {"status": "ok"}