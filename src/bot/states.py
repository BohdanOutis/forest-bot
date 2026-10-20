from aiogram.fsm.state import State, StatesGroup

class UploadDocFSM(StatesGroup):
    waiting_for_location = State()  # стан очікування локації від звіра

class QuestionLocationSG(StatesGroup):
    waiting_for_location = State()