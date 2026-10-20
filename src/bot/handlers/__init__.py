from aiogram import Router

from . import (
    common,
    document_upload,
    question,
    other,
)


def get_routers() -> list[Router]:
    return [
        common.router,
        document_upload.router,
        question.router,
        other.router,
    ]