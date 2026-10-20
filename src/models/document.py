from datetime import datetime
from typing import Optional
from sqlalchemy import (
    String,
    Text,
    BigInteger,
    Boolean,
    DateTime,
    Integer,
    Index,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from src.models import Base

class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)

    # Оригінальні значення для відображення
    topic: Mapped[str] = mapped_column(String(255), nullable=False)
    location: Mapped[str] = mapped_column(String(255), nullable=False)

    # Нормалізовані значення (casefold, strip, NFC) для пошуку дублікатів
    topic_key: Mapped[str] = mapped_column(String(255), nullable=False)
    location_key: Mapped[str] = mapped_column(String(255), nullable=False)

    priority: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    content_text: Mapped[str] = mapped_column(Text, nullable=False)
    
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    source: Mapped[str] = mapped_column(String(64), nullable=False)  # "google_drive" | "telegram"
    uploaded_by_tg_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    uploaded_by_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        server_default=func.now(), 
        nullable=False
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, 
        default=True, 
        server_default=text("true"), 
        nullable=False
    )

    __table_args__ = (
        # Частковий унікальний індекс: гарантує лише один АКТИВНИЙ документ з таким content_hash
        Index(
            "uq_active_content_hash",
            "content_hash",
            unique=True,
            postgresql_where=(is_active.is_(True)),
        ),
        # Частковий унікальний індекс: гарантує лише один АКТИВНИЙ документ для теми та локації
        Index(
            "uq_active_topic_location",
            "topic_key",
            "location_key",
            unique=True,
            postgresql_where=(is_active.is_(True)),
        ),
    )