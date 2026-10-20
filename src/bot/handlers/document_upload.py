import io
from pathlib import Path
from aiogram import Router, F, Bot
from aiogram.types import Message
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from src.bot.states import UploadDocFSM
from src.services.file_parser import extract_text
from src.services.groq_service import analyze_document
from src.services.document_service import save_or_update_document

logger = structlog.get_logger(__name__)
router = Router(name="document_upload")

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt"}

from aiogram import F

@router.message(UploadDocFSM.waiting_for_location, ~F.text)
async def incorrect_location(message: Message):
    await message.answer(
        text="Будь ласка, вкажіть місце коректно, використовуючи текст.\n"
        "Наприклад: хвойний ліс, болото або рукавичка",
    )

@router.message(F.document)
async def handle_document_message(
    message: Message,
    state: FSMContext,
    bot: Bot,
    session: AsyncSession
):
    """
    Хендлер отримання документа від звіра:
    1. Перевіряє розширення (.pdf, .docx, .txt).
    2. Завантажує файл через bot.download(message.document).
    3. Парсить текст через extract_text.
    4. Викликає analyze_document(text).
    5. Якщо location is None: зберігає у state, запитує локацію.
       Якщо location визначено: одразу зберігає документ.
    """
    user_id = message.from_user.id
    user_name = message.from_user.username or message.from_user.full_name
    doc = message.document
    filename = doc.file_name or "document.txt"
    extension = Path(filename).suffix.lower()

    log = logger.bind(
        user_id=user_id,
        user_name=user_name,
        filename=filename,
        extension=extension,
        file_size=doc.file_size
    )
    log.info("document_upload_received")

    # 1. Перевірка розширення
    if extension not in ALLOWED_EXTENSIONS:
        log.warning("unsupported_document_extension")
        await message.answer(
            f"❌ Формат файлу <code>{extension}</code> не підтримується.\n"
            "Лісовий архів приймає лише: <b>.pdf</b>, <b>.docx</b> або <b>.txt</b>."
        )
        return

    wait_msg = await message.answer("⏳ Читаю та аналізую лісовий документ...")

    try:
        # 2. Завантаження файлу
        file_io = io.BytesIO()
        await bot.download(doc, destination=file_io)
        file_bytes = file_io.getvalue()
        log.info("document_downloaded_to_memory", bytes_count=len(file_bytes))

        # 3. Парсинг тексту через extract_text
        text = extract_text(file_bytes, filename)
        if not text:
            log.warning("document_text_empty")
            await wait_msg.edit_text("⚠️ Не вдалося прочитати текст із документа. Переконайся, що файл не порожній.")
            return

        # 4. Аналіз через Groq
        topic, location, priority = await analyze_document(text)
        log.info("document_analyzed", topic=topic, location=location)

        # 5. Розгалуження за наявністю локації
        if location is None:
            # Зберігаємо дані у FSM
            await state.update_data(
                text=text,
                filename=filename,
                topic=topic,
                priority=priority,
            )
            await state.set_state(UploadDocFSM.waiting_for_location)
            log.info("fsm_switched_to_waiting_for_location")

            # Точний текст повідомлення за ТЗ
            await wait_msg.edit_text(
                "Дякую за пораду! Але в тексті не зазначено, для якого саме куточка лісу ці поради. "
                "Напиши, будь ласка, локацію (наприклад, \"березовий гай\" чи \"болото\"):"
            )
            return

        # Якщо локація визначена одразу
        result = await save_or_update_document(
            session=session,
            filename=filename,
            content=text,
            priority=priority,
            topic=topic,
            location=location,
            user=message.from_user,
            source="telegram",
        )

        saved_doc = result.doc

        if result.duplicate:
            await message.answer(
                f"⚠️ Цей документ уже є в архіві з таким самим вмістом!\n\n"
                f"📄 Файл: {saved_doc.filename}\n"
                f"📍 Локація: {saved_doc.location}"
            )
            await state.clear()
            log.info("fsm_state_cleared")
            return

        replace_notice = (
            "🔄 <i>Цей документ оновив і замінив попередні поради для цієї ж локації!</i>"
            if result.replaced else "✨ <i>Це новий куточок знань у нашому архіві!</i>"
        )

        log.info("document_saved_directly", doc_id=saved_doc.id, is_replaced=result.replaced)
        await wait_msg.edit_text(
            f"✅ <b>Поради успішно збережено в Лісовому Архіві!</b>\n\n"
            f"📄 <b>Файл:</b> {saved_doc.filename}\n"
            f"📍 <b>Локація:</b> {saved_doc.location}\n"
            f"🏷 <b>Тема:</b> {saved_doc.topic}\n\n"
            f"{replace_notice}"
        )

    except Exception as e:
        log.error("document_processing_error", error=str(e))
        await wait_msg.edit_text("❌ Сталася помилка під час обробки документа. Спробуй ще раз пізніше.")


@router.message(UploadDocFSM.waiting_for_location, Command("cancel"))
async def cancel_upload(message: Message, state: FSMContext):
    """Скасування завантаження документа."""
    user_id = message.from_user.id
    logger.info("document_upload_cancelled_by_user", user_id=user_id)
    await state.clear()
    await message.answer("❌ Збереження документа скасовано.")


@router.message(UploadDocFSM.waiting_for_location, F.text)
async def process_location_input(
    message: Message,
    state: FSMContext,
    session: AsyncSession
):
    """
    Обробник ручного введення локації від звіра:
    1. Дістає збережені дані з state.get_data().
    2. Викликає save_or_update_document(...).
    3. Очищає стан await state.clear().
    """
    user_id = message.from_user.id
    user_location = message.text.strip()
    log = logger.bind(user_id=user_id, input_location=user_location)
    log.info("process_location_input_received")

    if len(user_location) < 2:
        log.warning("location_input_too_short")
        await message.answer("Будь ласка, напиши зрозумілішу назву місця (хоча б кілька літер).")
        return

    data = await state.get_data()
    content = data.get("text") or data.get("content")
    filename = data.get("filename") or "document.txt"
    topic = data.get("topic") or "Загальні лісові поради"
    priority = data.get("priority") or 2

    try:
        result = await save_or_update_document(
            session=session,
            filename=filename,
            content=content,
            priority=priority,
            topic=topic,
            location=user_location,
            user=message.from_user,
            source="telegram",
        )

        saved_doc = result.doc

        if result.duplicate:
            await message.answer(
                f"⚠️ Цей документ уже є в архіві з таким самим вмістом!\n\n"
                f"📄 Файл: {saved_doc.filename}\n"
                f"📍 Локація: {saved_doc.location}"
            )
            await state.clear()
            log.info("fsm_state_cleared")
            return

        replace_notice = (
            "🔄 <i>Цей документ оновив і замінив попередні поради для цієї ж локації!</i>"
            if result.replaced else "✨ <i>Це новий куточок знань у нашому архіві!</i>"
        )

        log.info("document_saved_after_location_input", doc_id=saved_doc.id, is_replaced=result.replaced)
        await message.answer(
            f"🌲 <b>Дякую! Документ збережено:</b>\n\n"
            f"📄 <b>Файл:</b> {saved_doc.filename}\n"
            f"📍 <b>Локація:</b> {saved_doc.location}\n"
            f"🏷 <b>Тема:</b> {saved_doc.topic}\n\n"
            f"{replace_notice}"
        )

    except Exception as e:
        log.error("process_location_save_failed", error=str(e))
        await message.answer("❌ Не вдалося зберегти документ. Спробуй надіслати файл ще раз.")

    finally:
        await state.clear()
        log.info("fsm_state_cleared")