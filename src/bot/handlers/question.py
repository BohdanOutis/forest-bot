from datetime import datetime, timezone

from aiogram import Router, F, Bot
from aiogram.filters import StateFilter
from aiogram.types import Message
from aiogram.enums import ChatAction
from aiogram.fsm.context import FSMContext

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from src.bot.states import QuestionLocationSG
from src.models.message import MessageHistory
from src.services.document_service import get_active_documents, get_available_locations, get_active_documents_by_location
from src.services.groq_service import answer_question, analyze_question

logger = structlog.get_logger(__name__)
router = Router(name="question")

HISTORY_LIMIT = 6

@router.message(F.text, ~F.text.startswith("/"), ~StateFilter(QuestionLocationSG.waiting_for_location))
async def handle_text_question(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
):
    """
    Обробник текстових запитань від звірів:
    1. Зберігає питання в MessageHistory.
    2. Отримує останні повідомлення історії користувача.
    3. Дістає всі активні документи з БД.
    4. Передає в answer_question (Groq).
    5. Зберігає відповідь у MessageHistory.
    6. Надсилає відповідь звіру.
    """
    user_question = message.text.strip()

    available_locations = await get_available_locations(session)
    target_location = await analyze_question(user_question, available_locations)

    if not target_location:
        await state.update_data(pending_question=user_question)
        await state.set_state(QuestionLocationSG.waiting_for_location)
        await message.answer(
            "🌲 Хм, а про яку саме частину лісу чи рукавичку йдеться? "
            "Уточни місце, щоб я міг дати точну пораду!"
        )
        return

    await process_and_answer_question(message, session, user_question, target_location)


@router.message(QuestionLocationSG.waiting_for_location, F.text, ~F.text.startswith("/"))
async def process_location_response(message: Message, session: AsyncSession, state: FSMContext):
    target_location = message.text.strip()
    
    data = await state.get_data()
    user_question = data.get("pending_question")
    
    await state.clear()
    
    if not user_question:
        await message.answer("Ой, я забув, про що ти питав. Задай питання ще раз, будь ласка!")
        return

    await process_and_answer_question(message, session, user_question, target_location)


async def process_and_answer_question(message: Message, session: AsyncSession, user_question: str, target_location: str, bot: Bot = None):
    user_id = message.from_user.id
    log = logger.bind(user_id=user_id, question_preview=user_question[:80])
    log.info("handle_text_question_received")
    # 1. Зберігаємо питання в MessageHistory
    user_msg_entry = MessageHistory(
        tg_id=user_id,
        role="user",
        content=user_question,
        created_at=datetime.now(timezone.utc)
    )
    session.add(user_msg_entry)
    await session.commit()
    log.info("user_question_saved_to_history", history_id=user_msg_entry.id)

    # 2. Отримуємо останні повідомлення історії користувача
    history_query = (
        select(MessageHistory)
        .where(MessageHistory.tg_id == user_id)
        .order_by(MessageHistory.created_at.desc())
        .limit(HISTORY_LIMIT)
    )
    history_result = await session.execute(history_query)
    # Повертаємо в хронологічному порядку від старіших до новіших
    recent_history = list(reversed(history_result.scalars().all()))
    # Виключаємо поточне питання, щоб воно не дублювалося в історії діалогу
    context_history = [m for m in recent_history if m.id != user_msg_entry.id]
    log.info("recent_history_retrieved", history_count=len(context_history))

    # 3. Шукаємо документи для конкретної локації
    is_special_advice_available = True
    active_docs = await get_active_documents_by_location(session, target_location)
    if not active_docs:
        # Якщо специфічних документів немає — беремо всі документи
        is_special_advice_available = False
        active_docs = await get_active_documents(session)
        if not active_docs:
            await message.answer(
                f"🌲 У Лісовому Архіві поки немає ані спеціальних порад для «{target_location}», "
                f"ані загальних знань. Можеш додати їх сам!"
            )
            return

    log.info("documents_retrieved_for_location", location=target_location, docs_count=len(active_docs))

    # Вмикаємо статус "друкує...", якщо доступний bot
    current_bot = bot or message.bot
    if current_bot:
        try:
            await current_bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
        except Exception:
            pass

    # 4. Передаємо в answer_question (Groq)
    try:
        answer = await answer_question(
            question=user_question,
            context_docs=active_docs,
            history=context_history,
        )
        if target_location and not is_special_advice_available:
            prefix = f"Спеціальної поради для локації «{target_location}» в архіві немає, але ось загальні відомості:\n\n"
            answer = prefix + answer
    except Exception as e:
        log.error("groq_answer_generation_error", error=str(e))
        answer = "🌲 Ой, у лісовому архіві заплуталися стежки (помилка запиту). Спробуй перепитати трохи пізніше!"

    # 5. Зберігаємо відповідь у MessageHistory
    bot_msg_entry = MessageHistory(
        tg_id=user_id,
        role="assistant",
        content=answer,
        created_at=datetime.now(timezone.utc)
    )
    session.add(bot_msg_entry)
    await session.commit()
    log.info("bot_answer_saved_to_history", history_id=bot_msg_entry.id)

    # 6. Надсилаємо відповідь звіру
    await message.answer(answer)
    log.info("answer_sent_to_user")