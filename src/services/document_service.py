import re
import hashlib
import structlog
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, List, Tuple

from sqlalchemy import select, func, distinct
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from aiogram.types import User as TelegramUser

from src.models.document import Document

logger = structlog.get_logger(__name__)


def normalize(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFC", text).casefold()
    return re.sub(r"\s+", " ", t).strip()


def compute_content_hash(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


@dataclass
class SaveResult:
    doc: Document
    replaced: bool = False
    duplicate: bool = False


async def find_active_duplicate(session: AsyncSession, content: str) -> Document | None:
    """Викликай у хендлері ДО LLM: якщо повернуло документ, відповідай «Цей документ уже є в архіві»."""
    return await session.scalar(
        select(Document).where(
            Document.is_active.is_(True),
            Document.content_hash == compute_content_hash(content),
        )
    )


async def save_or_update_document(
    session: AsyncSession,
    filename: str,
    content: str,
    topic: str,
    location: str,
    priority: int,
    user: Optional[TelegramUser] = None,
    source: str = "telegram",
    uploaded_by_tg_id: Optional[int] = None,
    uploaded_by_name: Optional[str] = None,
) -> SaveResult:
    if user:
        uploaded_by_tg_id = user.id
        uploaded_by_name = f"@{user.username}" if user.username else user.full_name

    clean_topic = topic.strip()
    clean_location = location.strip()

    topic_key = normalize(topic)
    location_key = normalize(location)
    content_hash = compute_content_hash(content)

    log = logger.bind(
        filename=filename,
        topic=clean_topic,
        location=clean_location,
        priority=priority,
        source=source,
        uploaded_by_tg_id=uploaded_by_tg_id,
        uploaded_by_name=uploaded_by_name,
    )
    log.info("save_or_update_document_started")

    # 1. Точний дублікат серед усіх чинних документів
    dup = await find_active_duplicate(session, content)
    if dup:
        log.info("document_already_exists", doc_id=dup.id)
        return SaveResult(dup, duplicate=True)

    # 2. Чинний документ з тією самою парою (тема, місце)
    old = await session.scalar(
        select(Document).where(
            Document.is_active.is_(True),
            Document.topic_key == topic_key,
            Document.location_key == location_key,
        )
    )

    replaced = False

    if old:
        old.is_active = False
        await session.commit()
        replaced = True
        log.info("deactivating_previous_document_version", old_doc_id=old.id, old_filename=old.filename)

    new_doc = Document(
        filename=filename.strip(),
        topic=clean_topic,
        topic_key=topic_key,
        location=clean_location,
        location_key=location_key,
        content_hash=compute_content_hash(content),
        priority=priority,
        content_text=content,
        source=source,
        uploaded_by_tg_id=uploaded_by_tg_id,
        uploaded_by_name=uploaded_by_name,
        uploaded_at=datetime.now(timezone.utc),
        is_active=True,
    )
    session.add(new_doc)

    try:
        await session.commit()
        await session.refresh(new_doc)
        log.info(
            "document_saved_successfully",
            doc_id=new_doc.id,
            is_replaced=replaced,
        )
    except IntegrityError:
        await session.rollback()

        dup = await find_active_duplicate(session, content)
        if dup:
            return SaveResult(dup, duplicate=True)

        conflict_doc = await session.scalar(
            select(Document).where(
                Document.is_active.is_(True),
                Document.topic_key == topic_key,
                Document.location_key == location_key,
            )
        )
        if conflict_doc:
            return SaveResult(conflict_doc, replaced=True)
        
        raise

    return SaveResult(new_doc, replaced=replaced)


async def get_available_locations(session: AsyncSession) -> List[str]:
    log = logger.bind()
    log.info("fetching_available_locations_started")
    query = (
        select(distinct(Document.location))
        .where(Document.is_active == True)
    )
    result = await session.execute(query)

    locations = [row[0] for row in result.all() if row[0]]

    log.info("fetching_available_locations_completed")
    return locations


async def get_user_documents(tg_id: int, session: AsyncSession) -> List[Document]:
    log = logger.bind()
    log.info("fetching_user_documents_started")

    query = (
        select(Document)
        .where(Document.uploaded_by_tg_id == tg_id, Document.is_active == True)
        .order_by(Document.uploaded_at.desc())
    )
    result = await session.execute(query)
    docs = list(result.scalars().all())

    log.info("fetching_user_documents_completed", count=len(docs))
    return docs


async def delete_user_document(doc_id: int, session: AsyncSession) -> Document:
    log = logger.bind()
    log.info("deleting_document_started")
    
    query = select(Document).where(Document.id == doc_id, Document.is_active == True)
    result = await session.execute(query)
    doc = result.scalars().first()

    if not doc:
        log.info("document_not_found")
        return None
    
    doc.is_active = False
    await session.commit()
    await session.refresh(doc)

    log.info("deliting_document_completed", doc_id=doc.id)
    return doc


async def get_active_documents(session: AsyncSession) -> List[Document]:
    log = logger.bind()
    log.info("fetching_active_documents_started")

    query = (
        select(Document)
        .where(Document.is_active == True)
        .order_by(Document.uploaded_at.desc())
    )
    result = await session.execute(query)
    docs = list(result.scalars().all())

    log.info("fetching_active_documents_completed", count=len(docs))
    return docs

async def get_active_documents_by_location(session: AsyncSession, target_location: str) -> List[Document]:
    log = logger.bind()
    log.info("fetching_active_documents_by_location_started")

    query = (
        select(Document)
        .where(Document.is_active == True, Document.location == target_location)
        .order_by(Document.uploaded_at.desc())
    )
    result = await session.execute(query)
    docs = list(result.scalars().all())

    log.info("fetching_active_documents_by_location_completed", count=len(docs))
    return docs