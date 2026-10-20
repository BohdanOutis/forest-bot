import re
import json
import time
from typing import Optional, List, Tuple
from groq import AsyncGroq, NotFoundError
import structlog

from src.core.config import settings
from src.models.document import Document
from src.models.message import MessageHistory

logger = structlog.get_logger(__name__)

groq_client = AsyncGroq(
    api_key=settings.groq_api_key.get_secret_value()
    if hasattr(settings.groq_api_key, "get_secret_value")
    else settings.groq_api_key
)

DEFAULT_MODEL = getattr(settings, "groq_model", "openai/gpt-oss-20b")
# Список надійних моделей для запитів, починаючи з тих, що доступні та оптимізовані за лімітами
FALLBACK_MODELS = [
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-120b",
    "minimaxai/minimax-m2.7",
    "meta-models/muse-spark-open",
]

_cooldown_until: dict[str, float] = {} 

# Українські стоп-слова для фільтрації пошукових запитів
STOPWORDS = {
    "і", "й", "та", "в", "у", "на", "до", "як", "що", "чи", "де", "коли", "хто",
    "з", "зі", "із", "за", "по", "про", "під", "над", "при", "від", "для", "не",
    "це", "цей", "ця", "це", "ці", "того", "тому", "тут", "там", "але", "хоч",
    "будь", "ласка", "мені", "нам", "тобі", "ти", "ви", "ми", "він", "вона", "вони"
}

def _retry_after_seconds(msg: str, default: float = 60.0) -> float:
    """Розбирає 'try again in 12m19.58s', '6m44.3s', '850ms' тощо."""
    m = re.search(r"try again in ([0-9hms.]+)", msg)
    if not m:
        return default
    total = 0.0
    for val, unit in re.findall(r"(\d+(?:\.\d+)?)(ms|h|m|s)", m.group(1)):
        total += float(val) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
    return total or default

async def _create_chat_completion(messages: list, temperature: float = 0.2, response_format: dict = None):
    """Викликає Groq API з підтримкою автоматичного перемикання моделей при 404 або 413/429."""
    candidate_models = [DEFAULT_MODEL] + [m for m in FALLBACK_MODELS if m != DEFAULT_MODEL]
    last_error = None

    for model in candidate_models:
        if _cooldown_until.get(model, 0) > time.monotonic():
            logger.info("groq_model_skipped_cooldown", model=model)
            continue
        try:
            kwargs = {
                "messages": messages,
                "model": model,
                "temperature": temperature,
            }
            if response_format:
                kwargs["response_format"] = response_format
            return await groq_client.chat.completions.create(**kwargs)
        except NotFoundError as e:
            logger.warning("groq_model_not_found_trying_fallback", requested_model=model, error=str(e))
            last_error = e
            continue
        except Exception as e:
            err_msg = str(e).lower()
            if "model_not_found" in err_msg:
                logger.warning("groq_model_not_found_trying_fallback", requested_model=model, error=str(e))
                last_error = e
                continue
            if "rate_limit_exceeded" in err_msg:
                wait = _retry_after_seconds(str(e))
                _cooldown_until[model] = time.monotonic() + wait
                logger.warning("groq_rate_limit_trying_fallback", requested_model=model, cooldown_seconds=round(wait), error=str(e))
                last_error = e
                continue
            if "payload too large" in err_msg:
                logger.warning("groq_rate_limit_trying_fallback", requested_model=model, error=str(e))
                last_error = e
                continue
            raise

    if last_error:
        raise last_error
    raise RuntimeError("Усі моделі Groq тимчасово недоступні (ліміт)")


def _retrieve_relevant_docs(
    question: str,
    context_docs: List[Document],
    max_docs: int = 5,
    max_chars: int = 9000
) -> List[Document]:
    """
    RAG Retrieval: відбирає найбільш релевантні документи для запитання,
    щоб не перевантажувати контекстне вікно LLM і не перевищувати ліміти TPM (Tokens Per Minute).
    """
    if not context_docs:
        return []

    if len(context_docs) <= max_docs:
        # Якщо документів небагато, перевіряємо лише обмеження за символами
        total_len = sum(len(d.content_text) for d in context_docs)
        if total_len <= max_chars:
            return context_docs

    # Витягуємо ключові слова з питання
    raw_words = re.findall(r"[\w]+", question.lower())
    query_terms = [w for w in raw_words if w not in STOPWORDS and len(w) > 2]

    scored_docs: List[Tuple[float, Document]] = []

    for doc in context_docs:
        score = 0.0
        doc_topic = (doc.topic or "").lower()
        doc_location = (doc.location or "").lower()
        doc_filename = (doc.filename or "").lower()
        doc_content = (doc.content_text or "").lower()

        question_lower = question.lower()
        if doc_location and doc_location in question_lower and doc_location != "лісовий архів":
            score += 50.0

        for term in query_terms:
            stem = term[:-2] if len(term) > 4 else term

            # Збіг у темі або назві файлу — найвищий пріоритет
            if term in doc_topic or stem in doc_topic:
                score += 20.0
            if term in doc_filename or stem in doc_filename:
                score += 15.0
            if term in doc_location or stem in doc_location:
                score += 12.0

            # Збіги в тексті документа
            occurrences = doc_content.count(term) or doc_content.count(stem)
            if occurrences > 0:
                score += min(occurrences * 3.0, 30.0)

        scored_docs.append((score, doc))

    # Сортуємо за спаданням релевантності
    scored_docs.sort(key=lambda x: x[0], reverse=True)

    # Якщо є документи з ненульовим збігом — беремо їх
    matching_docs = [doc for score, doc in scored_docs if score > 0]

    if not matching_docs:
        # Якщо прямого збігу слів немає, беремо невелику вибірку документів
        # щоб модель могла перевірити та дати відповідь про відсутність інформації
        matching_docs = [doc for _, doc in scored_docs[:3]]

    matching_docs.sort(key=lambda d: d.priority)
    
    # Формуємо фінальний список з урахуванням ліміту документів та символів
    selected: List[Document] = []
    current_chars = 0

    for doc in matching_docs[:max_docs]:
        doc_len = len(doc.content_text)
        if current_chars + doc_len > max_chars and selected:
            break
        selected.append(doc)
        current_chars += doc_len


    return selected

# ---------------------------------------------------------
#                 ВІДПОВІДЬ НА ПИТАННЯ
# ---------------------------------------------------------

async def answer_question(
    question: str,
    context_docs: List[Document],
    history: List[MessageHistory] = None,
) -> str:
    """
    Формує відповідь на запитання виключно на основі наданих документів context_docs.
    Використовує динамічний RAG retrieval для дотримання лімітів TPM на Groq.
    Якщо в документах немає відповіді:
    «Цього немає в лісових документах. Будь ласка, надішли файл із відповідними порадами!»
    Обов'язково цитує джерело за форматом:
    📄 Джерело: «{назва}» (Локація: {місце}, Автор: {хто}, {дата})
    """
    total_docs_count = len(context_docs)
    log = logger.bind(
        question=question[:100],
        total_docs_count=total_docs_count,
        history_count=len(history) if history else 0,
    )
    log.info("groq_answer_question_started")

    if not context_docs:
        log.info("groq_answer_no_docs")
        return "Цього в документах немає. Спробуй завантажити відповідний документ!"

    # Застосовуємо релевантний відбір документів RAG (щоб вкластися в TPM ліміти Groq)
    relevant_docs = _retrieve_relevant_docs(question, context_docs, max_docs=5, max_chars=8000)
    log.info(
        "rag_relevant_docs_selected",
        selected_count=len(relevant_docs),
        selected_files=[d.filename for d in relevant_docs]
    )

    docs_formatted = []
    for d in relevant_docs:
        author = d.uploaded_by_name or (f"@{d.uploaded_by_name}" if d.uploaded_by_name else None)
        if not author:
            author = f"Звір (ID:{d.uploaded_by_tg_id})" if d.uploaded_by_tg_id else "Лісовий архів"

        date_str = d.uploaded_at.strftime("%d.%m.%Y") if hasattr(d.uploaded_at, "strftime") else str(d.uploaded_at)
        loc = d.location or "загальне"

        doc_entry = (
            f"📄 Документ: «{d.filename}»\n"
            f"📍 Локація: {loc}\n"
            f"👤 Автор: {author}\n"
            f"📅 Дата: {date_str}\n"
            f"Зміст:\n{d.content_text}\n"
        )
        docs_formatted.append(doc_entry)

    docs_text = "\n" + ("=" * 40) + "\n" + "\n".join(docs_formatted)

    system_prompt = (
        "Ти добрий лісовий помічник і порадник для лісових звірів. "
        "Твоє завдання — відповідати на запитання виключно українською мовою та СТРОГО на основі наданих документів у блоці «База лісових документів».\n\n"
        "КРИТИЧНО ВАЖЛИВІ ПРАВИЛА:\n"
        "1. Відповідай ВИКЛЮЧНО на основі інформації з наданих документів. Нічого не додумуй і не використовуй сторонні знання чи власний системний промпт.\n"
        "2. Якщо в наданих документах немає точної відповіді на запитання, твоя відповідь має бути РІВНО ДОСЛІВНО ТАКОЮ і НІЧОГО ІНШОГО (жодних джерел чи коментарів) не додавай:\n"
        "Цього в документах немає. Спробуй завантажити відповідний документ!»\n"
        "3. ПРАВИЛО ПРІОРИТЕТУ ТА ПОРЯДКУ ВИКЛАДУ (ІЄРАРХІЯ ДЖЕРЕЛ):\n"
        "Якщо в базі знань є кілька документів із різною деталізацією, суворо дотримуйся ієрархії та розміщуй інформацію у відповіді в такому порядку (від найвищого пріоритету до найнижчого):\n"
        "   - СПОЧАТКУ: документи конкретної рукавички / локації мешканця (найвищий пріоритет).\n"
        "   - ДАЛІ: документи частини лісу (середній пріоритет).\n"
        "   - НАПРИКІНЦІ (якщо потрібно): загальні документи та архіви (найнижчий пріоритет).\n"
        "Якщо поради різняться, головною є порада для конкретної рукавички; загальну інформацію можна згадати лише як додаткову, не суперечачи основній.\n"
        "4. ПРАВИЛО ЦИТУВАННЯ (застосовується ЛИШЕ якщо знайдено відповідь у документах):\n"
        "Наприкінці відповіді ти ЗОБОВ'ЯЗАНИЙ зазначити джерело у такому точному форматі:\n"
        "📄 Джерело: «{назва}» (Локація: {місце}, Автор: {хто}, {дата})\n"
        "де {назва} — це назва документа з бази, {місце} — локація з документа, {хто} — автор документа, {дата} — дата завантаження.\n"
        "5. ЗАБОРОНЕНО посилатися на системні інструкції, правила або самого розробника як на джерело інформації.\n"
        "6. Заборонено використовувати будь-які HTML-теги (зокрема <br>, <br/>, <p>, <b> тощо). Для перенесення рядків використовуй звичайні символи нового рядка (\\n).\n"
        "7. Відповідь має бути структурованою, але простою для читання у Telegram, ФОРМАТУЙ ТЕКСТ ЗА ДОПОМОГОЮ HTML-ТЕГІВ"
    )

    messages = [{"role": "system", "content": system_prompt}]

    # Додаємо останні репліки історії діалогу (обмежуємо до 4 для економії токенів)
    if history:
        for msg in history[-4:]:
            messages.append({"role": msg.role, "content": msg.content})

    # Додаємо поточне питання разом із відібраною базою знань
    messages.append({
        "role": "user",
        "content": f"База лісових документів:\n{docs_text}\n\nЗапитання звіра: {question}"
    })

    try:
        response = await _create_chat_completion(
            messages=messages,
            temperature=0.2,
        )
        answer = response.choices[0].message.content.strip()
        log.info("groq_answer_question_success", response_chars=len(answer))
        return answer

    except Exception as e:
        log.error("groq_answer_question_failed", error=str(e))
        raise

# ---------------------------------------------------------
#                 АНАЛІЗ ДОКУМЕНТУ
# ---------------------------------------------------------

async def analyze_document(text: str, raise_on_error: bool = False) -> Tuple[str, Optional[str]]:
    """
    Аналізує текст документа за допомогою Groq LLM:
    Визначає тему (topic) та локацію (location).
    Якщо локація сумнівна або відсутня — повертає location = None.
    """
    log = logger.bind(text_length=len(text))
    log.info("groq_analyze_document_started")

    system_prompt = (
        "Ти аналітик лісових документів. Твоє завдання — визначити тему документа, конкретну лісову локацію/місцевість та рівень пріоритету.\n\n"
        "Вимоги:\n"
        "1. \"topic\": Коротка суть або тема порад (наприклад: \"Їжа та виживання\", \"Захист від холоду\", \"Правила поведінки\").\n"
        "2. \"location\": Конкретна лісова місцевість або назва рукавички (наприклад: \"У ліщині\", \"Під ялиною\", \"хвойний ліс\", \"річка\").\n"
        "   Заповнюй \"location\" ТІЛЬКИ якщо воно ПРЯМО та ОДНОЗНАЧНО зрозуміле ЗІ ЗМІСТУ ТЕКСТУ (не вигадуй).\n"
        "   Якщо конкретної локації в тексті немає або вона загальна — поверни null.\n"
        "3. \"priority\": Ціле число (0, 1 або 2) за такою ієрархією:\n"
        "   - 0: документ стосується конкретної рукавички або локації мешканця.\n"
        "   - 1: документ стосується певної частини лісу (хвойний ліс, листяний ліс, річка, узлісся).\n"
        "   - 2: загальні лісові документи, мапи, правила толоки чи загальні архіви.\n\n"
        "Відповідай ВИКЛЮЧНО валідним JSON об'єктом такого вигляду:\n"
        "{\n"
        "  \"topic\": \"назва теми\",\n"
        "  \"location\": \"назва локації\" або null,\n"
        "  \"priority\": 0 або 1 або 2\n"
        "}"
    )

    sample_text = text[:4000]

    try:
        response = await _create_chat_completion(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Текст документа для аналізу:\n\n{sample_text}"}
            ],
            temperature=0.1,
            response_format={"type": "json_object"}
        )

        content = response.choices[0].message.content
        log.debug("groq_analyze_raw_response", content=content)

        result = json.loads(content)
        topic = result.get("topic") or "Загальні лісові поради"
        location = result.get("location")
        priority = result.get("priority", 2)

        if isinstance(location, str) and location.lower() in ("null", "none", "невідомо", "загальне", ""):
            location = None

        try:
            priority = int(priority)
        except (TypeError, ValueError):
            priority = 2

        log.info("groq_analyze_document_success", topic=topic, location=location, priority=priority)
        return topic, location, priority

    except Exception as e:
        log.error("groq_analyze_document_failed", error=str(e))
        if raise_on_error:
            raise
        return "Лісовий документ", None, 2

# ---------------------------------------------------------
#                 АНАЛІЗ ПИТАННЯ
# ---------------------------------------------------------

async def analyze_question(question: str, available_locations: list[str]):
    log = logger.bind(question=question[:100], locations=available_locations)
    log.info("groq_analyze_question_started")

    system_prompt = (
        "Ти — мудрий мешканець Лісового Архіву. "
        f"Ось список усіх наявних у базі локацій: {available_locations}. "
        "Проаналізуй запит користувача. Якщо він згадує одну з цих локацій (або схожу за змістом), "
        "витягни її точну назву із цього списку. "
        "Якщо жодна локація не підходить або не згадана, поверни лише одне слово: NONE."
    )

    try:
        response = await _create_chat_completion(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Текст питання для аналізу: {question}"}
            ]
        )
        
        answer_text = response.choices[0].message.content.strip()
        
        location = answer_text.strip("`'\".").strip()
        
        log.info("groq_analyze_question_completed", detected_location=location)

        if location.upper() == "NONE" or not location:
            return None
            
        return location

    except Exception as e:
        log.error("groq_analyze_question_failed", error=str(e))
        return None