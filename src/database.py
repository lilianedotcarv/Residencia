"""Configuração do banco assíncrono e modelos de persistência do chat."""

from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATABASE_URL = f"sqlite+aiosqlite:///{(PROJECT_ROOT / 'database.db').as_posix()}"

engine = create_async_engine(DATABASE_URL)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    """Classe base para os modelos SQLAlchemy da aplicação."""


class UserSession(Base):
    """Mapeia tokens JWT (ou seus hashes/JTIs) aos IDs de conversas."""

    __tablename__ = "users_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    jwt_token_hash: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )
    conversation_id: Mapped[str] = mapped_column(
        String(255), nullable=False, index=True
    )
    user_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ChatSession(Base):
    """Metadados de uma sessão do chatbot."""

    __tablename__ = "chat_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )
    user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    user_data: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="active", nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    logs: Mapped[list["UsageLog"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    metrics: Mapped[list["Metric"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class ChatMessage(Base):
    """Mensagem individual associada a uma conversa persistente."""

    __tablename__ = "chat_messages"
    __table_args__ = (
        Index("ix_chat_messages_conversation_created", "conversation_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class UsageLog(Base):
    """Registra as atividades realizadas no chatbot."""

    __tablename__ = "usage_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    session: Mapped["ChatSession"] = relationship(back_populates="logs")


class Metric(Base):
    """Armazena métricas de utilização e desempenho."""

    __tablename__ = "metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str | None] = mapped_column(String(50), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    session: Mapped["ChatSession | None"] = relationship(back_populates="metrics")


CACHE_DOMAINS = ("account", "users", "tickets", "articles", "categories", "timeline")
CACHE_TABLES = {
    domain: Table(
        f"cache_{domain}",
        Base.metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("source_key", String(255), nullable=True),
        Column("data", JSON, nullable=False),
        UniqueConstraint("source_key", name=f"uq_cache_{domain}_source_key"),
    )
    for domain in CACHE_DOMAINS
}


async def init_db() -> None:
    """Cria as tabelas mapeadas que ainda não existem (fallback sem Alembic)."""
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """Fornece uma sessão por operação e a fecha ao terminar."""
    async with AsyncSessionLocal() as session:
        yield session