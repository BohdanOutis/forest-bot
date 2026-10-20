import asyncio
from pathlib import Path
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from src.models.document import Document
from src.services.file_parser import extract_text, OCRUnavailableError
from src.services.groq_service import analyze_document
from src.services.document_service import save_or_update_document

logger = structlog.get_logger(__name__)

# Директорія із початковими документами (seed) у корені проєкту
LOCAL_SEED_DIR = Path(__file__).resolve().parent.parent.parent / "initial_docs"


async def _process_seed_file(session: AsyncSession, file_path: Path) -> None:
    """Повертає статус: ok, skipped, deferred або failed."""
    filename = file_path.name
    log = logger.bind(filename=filename, file_size=file_path.stat().st_size)

    # 0. Уже імпортований файл: без жодного звернення до LLM
    already = await session.scalar(
        select(Document.id)
        .where(Document.source == "google_drive", Document.filename == filename)
        .limit(1)
    )
    if already:
        log.info("seed_file_skipped_already_imported", doc_id=already)
        return "skipped"

    log.info("seed_file_processing_started")

    # 1. Зчитування байтів та витяг тексту
    try:
        file_bytes = file_path.read_bytes()
        text = await asyncio.to_thread(extract_text, file_bytes, filename, True)
    except OCRUnavailableError as e:
        log.warning("seed_file_deferred_ocr_unavailable", error=str(e))
        return "deferred"
    except Exception as e:
        log.error("seed_file_extraction_failed", error=str(e))
        return "failed"

    if not text.strip():
        log.warning("seed_file_empty_content")
        return "failed"

    # 2. Визначення теми й локації
    try:
        topic, location, priority = await analyze_document(text, raise_on_error=True)
    except Exception as e:
        log.warning("seed_file_deferred_analysis_failed", error=str(e))
        return "deferred"
    if not location:
        location = "лісовий архів"

    log.info("seed_file_analyzed", topic=topic, location=location)

    # 3. Збереження в базу
    result = await save_or_update_document(
        session=session,
        filename=filename,
        content=text,
        topic=topic,
        location=location,
        priority=priority,
        source="google_drive",
        uploaded_by_name="Лісовий архів",
    )

    log.info(
        "seed_file_synced_to_db",
        doc_id=result.doc.id,
        is_replaced=result.replaced,
        duplicate=result.duplicate,
    )
    return "ok"


async def sync_drive_documents(session: AsyncSession) -> None:
    logger.info("sync_seed_documents_started")

    if not LOCAL_SEED_DIR.exists():
        logger.error("local_seed_dir_not_found", path=str(LOCAL_SEED_DIR))
        return

    local_files = [f for f in LOCAL_SEED_DIR.glob("*.*") if f.is_file()]
    logger.info("syncing_from_local_seed_folder", count=len(local_files))

    counts = {"ok": 0, "skipped": 0, "deferred": 0, "failed": 0}
    deferred_files = []
    for file_path in local_files:
        status = await _process_seed_file(session, file_path)
        counts[status] += 1
        if status == "deferred":
            deferred_files.append(file_path.name)

    if deferred_files:
        logger.warning("seed_files_deferred_until_next_start", files=deferred_files)
    logger.info("sync_seed_documents_finished", **counts)