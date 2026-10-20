from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command, CommandStart
from sqlalchemy.ext.asyncio import AsyncSession
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
import structlog

from src.services.document_service import get_active_documents, get_user_documents, delete_user_document

logger = structlog.get_logger(__name__)
router = Router(name="common")


HELP_TEXT = (
    "🌲 <b>Привіт, друже-звіре!</b>\n\n"
    "Я — помічник «Лісового архіву». Відповідаю на запитання <b>лише за документами з порадами</b> "
    "і завжди кажу, з якого документа взяв пораду.\n\n"
    "📌 <b>Що я вмію:</b>\n"
    "1. <b>Відповідати на запитання.</b> Просто напиши питання текстом, наприклад: "
    "<i>«Що їсти в хвойному лісі?»</i> або <i>«Які небезпеки поруч із рукавичкою під дубом?»</i>\n"
    "2. <b>Приймати поради.</b> Надішли файл <b>.pdf</b>, <b>.docx</b> або <b>.txt</b> із текстом. "
    "Я визначу, для якого місця ці поради. Якщо з документа це не зрозуміло, перепитаю тебе.\n"
    "3. <b>/mydocs</b> — показати твої документи й видалити їх, якщо потрібно.\n"
    "4. <b>/docs</b> — показати всі чинні документи архіву.\n"
    "5. <b>/help</b> — показати це повідомлення.\n\n"
    "ℹ️ <b>Як я відповідаю:</b>\n"
    "• Спершу даю пораду, що стосується саме того місця, про яке ти питаєш "
    "(конкретне місце біля рукавички, потім частина лісу), і лише потім загальну.\n"
    "• Якщо в документах відповіді немає, так і скажу.\n"
    "• Новіший документ замінює старий лише для тієї самої теми й того самого місця.\n\n"
    "👥 <b>Важливо знати:</b> усі звірі рівні. Документи, які ти надсилаєш, бачать і використовують усі, "
    "а я запам'ятовую, хто й коли їх подав, щоб було кому подякувати."
)


@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start_or_help(message: Message):
    """Вітальне повідомлення з інструкціями (/start та /help)."""
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.full_name
    logger.info("start_or_help_command_triggered", user_id=user_id, username=username)
    await message.answer(HELP_TEXT)


@router.callback_query(lambda c: c.data and c.data.startswith("del_doc:"))
async def process_delete_document(callback: CallbackQuery, session: AsyncSession):
    doc_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id

    doc = await delete_user_document(doc_id, session)

    if not doc:
        await callback.answer("❌ Цей документ уже видалений або не існує.", show_alert=True)
        return

    if doc.source == "google_drive" or (doc.uploaded_by_tg_id and doc.uploaded_by_tg_id != user_id):
        await callback.answer("⛔ Ти можеш видаляти лише власні документи! Документи «Лісового архіву» недоторканні.", show_alert=True)
        return

    await callback.message.edit_text(
        f"✅ Документ <b>{doc.filename}</b> успішно видалено з твоїх порад!",
        parse_mode="HTML"
    )
    await callback.answer()
    

@router.message(Command("mydocs"))
async def cmd_mydocs(message: Message, session: AsyncSession):
    """
    Виводить список документів, які завантажив саме цей користувач,
    з можливістю їх видалення.
    """
    user_id = message.from_user.id
    
    # Шукаємо активні документи, які завантажив цей користувач
    user_docs = await get_user_documents(user_id, session)

    if not user_docs:
        await message.answer("📁 Ти ще не завантажував жодних власних документів до архіву.")
        return

    text = "🌲 <b> твої завантажені документи:</b>\nВибери документ, який хочеш видалити:"
    
    # Створюємо клавіатуру з кнопками для кожного документа
    keyboard_buttons = []
    for doc in user_docs:
        loc = doc.location or "загальне"
        btn_text = f"🗑 Видалити: {doc.filename} [{loc}]"
        # У callback_data передаємо id документа
        keyboard_buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"del_doc:{doc.id}")])

    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.message(Command("docs"))
async def cmd_docs(message: Message, session: AsyncSession):
    """
    Виводить список усіх актуальних документів (з пагінацією/розбивкою, якщо текст занадто довгий):
    📚 1. [хвойний ліс] Що їсти в хвойному лісі.txt (Лісовий архів)
    📚 2. [болото] Як не втопитися.pdf (подав @Їжак, 15.10.2026)
    """
    user_id = message.from_user.id
    logger.info("docs_command_triggered", user_id=user_id)

    docs = await get_active_documents(session)

    if not docs:
        await message.answer("📚 У Лісовому Архіві поки що немає активних документів. Ти можеш надіслати перший!")
        return

    header = "🌲 <b>Актуальні документи Лісового Архіву:</b>\n"
    lines = [header]
    
    for i, doc in enumerate(docs, 1):
        loc = doc.location or "загальне"
        
        # Визначаємо авторство та дату
        if doc.source == "google_drive" or (not doc.uploaded_by_name and not doc.uploaded_by_tg_id):
            author_info = "(Лісовий архів)"
        else:
            author_name = doc.uploaded_by_name or f"ID:{doc.uploaded_by_tg_id}"
            date_str = doc.uploaded_at.strftime("%d.%m.%Y") if hasattr(doc.uploaded_at, "strftime") else str(doc.uploaded_at)
            author_info = f"(подав {author_name}, {date_str})"

        lines.append(f"📚 {i}. [{loc}] {doc.filename} {author_info}")

    logger.info("docs_list_rendered", total_docs=len(docs))

    # Збираємо повідомлення, слідкуючи за лімітом Telegram (близько 4000 символів на одне повідомлення)
    current_chunk = ""
    for line in lines:
        # Якщо додавання нового рядка перевищить ліміт, відправляємо поточний шматочок і починаємо новий
        if len(current_chunk) + len(line) + 1 > 4000:
            await message.answer(current_chunk, parse_mode="HTML")
            current_chunk = line
        else:
            current_chunk += ("\n" if current_chunk else "") + line

    # Відправляємо залишок, що залишився
    if current_chunk:
        await message.answer(current_chunk, parse_mode="HTML")
