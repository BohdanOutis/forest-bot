from aiogram import Router
from aiogram.types import Message

router = Router()

@router.message()
async def other(message: Message):
    await message.answer("Не можу прочитати цей файл. Надішли PDF, Word або txt із текстом")