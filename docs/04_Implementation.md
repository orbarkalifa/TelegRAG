# Implementation Guide

## 1. Project Directory Map
Ensure your project looks exactly like this before running any commands.

```text
/rag-bot
├── docs/                    # (The documentation we just created)
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI Webhook (Entry Point)
│   ├── worker.py            # Celery Worker (The Brain)
│   ├── models.py            # SQLModel Database Schemas
│   ├── database.py          # DB Connection (Async & Sync)
│   ├── rag_engine.py        # RAG Logic (LangChain)
│   ├── logger_setup.py      # Structlog Configuration
│   └── ingest.py            # Script to upload PDFs
├── .env                     # Secrets (API Keys, DB URLs)
├── .gitignore               # Exclude .env, __pycache__, venv/
├── docker-compose.yml       # Infrastructure
└── requirements.txt         # Dependencies
```
## 2. Infrastructure (docker-compose.yml)

Save this file in the root.
```YAML

version: '3.8'

services:
  # 1. Long-Term Memory (Postgres)
  db:
    image: postgres:15-alpine
    restart: always
    environment:
      POSTGRES_USER: ${DB_USER}
      POSTGRES_PASSWORD: ${DB_PASSWORD}
      POSTGRES_DB: ${DB_NAME}
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${DB_USER} -d ${DB_NAME}"]
      interval: 5s
      timeout: 5s
      retries: 5
    networks:
      - rag_net

  # 2. Short-Term Memory & Broker (Redis)
  redis:
    image: redis:7-alpine
    restart: always
    networks:
      - rag_net
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5

  # 3. Knowledge Base (Qdrant)
  qdrant:
    image: qdrant/qdrant:latest
    restart: always
    ports:
      - "6333:6333" # Exposed for local ingest script
    volumes:
      - qdrant_data:/qdrant/storage
    networks:
      - rag_net

  # 4. API Gateway (FastAPI)
  bot_api:
    build: .
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000
    env_file: .env
    restart: always
    depends_on:
      redis:
        condition: service_healthy
      db:
        condition: service_healthy
    ports:
      - "8000:8000" # Public Port for Webhook
    networks:
      - rag_net

  # 5. The Brain (Celery Worker)
  worker:
    build: .
    command: celery -A app.worker.celery_app worker --loglevel=info
    env_file: .env
    restart: always
    depends_on:
      redis:
        condition: service_healthy
      db:
        condition: service_healthy
      qdrant:
        condition: service_started
    networks:
      - rag_net

volumes:
  postgres_data:
  qdrant_data:

networks:
  rag_net:
```
## 3. Dependencies (requirements.txt)

Save this file in the root.
```Plaintext

# --- Core Frameworks ---
fastapi==0.104.1
uvicorn[standard]==0.24.0
celery==5.3.6
redis==5.0.1
aiogram==3.2.0

# --- Database ---
sqlmodel==0.0.14
asyncpg==0.29.0
psycopg2-binary==2.9.9 # For Sync Celery worker

# --- AI & RAG ---
langchain==0.1.0
langchain-google-genai==0.0.5
langchain-qdrant==0.0.1
qdrant-client==1.7.0
google-generativeai==0.3.2

# --- Utilities ---
python-dotenv==1.0.0
structlog==23.2.0
python-json-logger==2.0.7
httpx==0.25.2
```
## 4. Configuration (.env)

Create a .env file. DO NOT COMMIT THIS TO GIT.
```Ini, TOML

# --- Telegram ---
BOT_TOKEN=123456789:ABCDefGhiJklMnoPqrStuVwxYz
# Generate a secret string for security (e.g. `openssl rand -hex 32`)
TELEGRAM_SECRET_TOKEN=my-super-secret-webhook-token

# --- Google Gemini ---
GOOGLE_API_KEY=AIzaSyD...

# --- Database (Internal Docker DNS) ---
DB_USER=postgres
DB_PASSWORD=securepassword
DB_NAME=rag_db
# Async for FastAPI
DATABASE_URL_ASYNC=postgresql+asyncpg://postgres:securepassword@db:5432/rag_db
# Sync for Celery
DATABASE_URL_SYNC=postgresql+psycopg2://postgres:securepassword@db:5432/rag_db
# --- Infrastructure ---
REDIS_URL=redis://redis:6379/0
QDRANT_URL=http://qdrant:6333
```

## 5. Deployment Checklist

* Initialize Git: git init
* Add Ignore: Create .gitignore containing .env, venv/, __pycache__/.
* Build Containers: docker-compose up --build -d
* Verify Logs: docker-compose logs -f (Ensure no crashes).
* Expose Port: Use Ngrok for local dev: ngrok http 8000.
* Set Webhook:
    ```Bash
    curl -F "url=[https://YOUR-NGROK.ngrok-free.app/webhook](https://YOUR-NGROK.ngrok-free.app/webhook)" \
         -F "secret_token=my-super-secret-webhook-token" \
         [https://api.telegram.org/bot](https://api.telegram.org/bot)<YOUR_BOT_TOKEN>/setWebhook
    ```
---