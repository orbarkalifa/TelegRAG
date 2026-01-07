import os
from sqlalchemy.orm import sessionmaker
from sqlmodel import create_engine, SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy.ext.asyncio import create_async_engine

# 1. Load URLs from environment
# We default to the docker service name "db"
DATABASE_URL_ASYNC = os.getenv("DATABASE_URL_ASYNC", "postgresql+asyncpg://postgres:securepassword@db:5432/rag_db")
DATABASE_URL_SYNC = os.getenv("DATABASE_URL_SYNC", "postgresql+psycopg2://postgres:securepassword@db:5432/rag_db")

# 2. Async Engine (For FastAPI)
async_engine = create_async_engine(DATABASE_URL_ASYNC, echo=False, future=True)

async def get_async_session():
    async_session = sessionmaker(
        bind=async_engine, class_=AsyncSession, expire_on_commit=False
    )
    async with async_session() as session:
        yield session

# 3. Sync Engine (For Celery Worker)
# Celery is synchronous, so it needs a standard psycopg2 connection
sync_engine = create_engine(DATABASE_URL_SYNC, echo=False)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=sync_engine)

def get_sync_session():
    """Dependency for Celery Tasks to get a DB session"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# 4. Initialization
async def init_db():
    """Creates tables if they don't exist (Run on startup)"""
    async with async_engine.begin() as conn:
        # This creates the tables defined in app/models.py
        await conn.run_sync(SQLModel.metadata.create_all)