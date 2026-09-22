"""Интеграционные проверки для HEADMAN_TG_BOT.

Запуск из корня репозитория:
    .venv/bin/python tests/test_bot.py

Тест использует реальный файл БД бота, но регистрирует отдельного
тестового пользователя и удаляет его в конце.
"""

import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import logging
logging.disable(logging.CRITICAL)

import bot
from database import Database

TEST_TG_ID = 987_654_321_000
TEST_TG_ID2 = 987_654_321_001

db = Database()


class FakeUser:
    def __init__(self, uid: int):
        self.id = uid
        self.username = "test_user"
        self.first_name = "Тест"
        self.last_name = "Тестов"
        self.language_code = "ru"


class FakeMessage:
    def __init__(self) -> None:
        self.edited_text = None
        self.answered = []
        self.deleted = False

    async def edit_text(self, text: str, reply_markup=None) -> None:
        self.edited_text = text
        self.reply_markup = reply_markup

    async def answer(self, text: str = "", **kwargs) -> None:
        self.answered.append(text)

    async def delete(self) -> None:
        self.deleted = True


class FakeCallback:
    def __init__(self, data: str, uid: int = TEST_TG_ID) -> None:
        self.data = data
        self.from_user = FakeUser(uid)
        self.message = FakeMessage()
        self.toasts = []

    async def answer(self, *args, **kwargs) -> None:
        self.toasts.append((args, kwargs))


def cleanup_user(uid: int) -> None:
    from sqlalchemy import delete
    from database import BotUser
    from sqlalchemy.orm import Session
    with Session(db.engine) as session:
        session.execute(delete(BotUser).where(BotUser.telegram_id == uid))
        session.commit()


def run(coro):
    return asyncio.run(coro)


def test_01_db_and_formatters() -> None:
    assert db.list_courses() == [1, 2, 3, 4, 5]
    assert "ИС 9-13" in db.list_groups_by_course(4)
    assert db.get_course_by_group("ИС 9-13") == 4

    assert bot._nice_teacher("СОЛОВЬЕВА Ф.А.") == "Ф.А. Соловьева"
    assert bot._nice_teacher("ДУБЕНЧУК А.В.") == "А.В. Дубенчук"
    assert bot._nice_teacher("Кто-то") == "Кто-то"
    assert bot._nice_discipline("АРХИТЕКТУРА АППАРАТНЫХ СРЕДСТВ") == "Архитектура аппаратных средств"
    assert bot._nice_discipline("МДК.02.01 ТЕХНОЛОГИЯ РАЗРАБОТКИ ПРОГРАММ") == "МДК.02.01 Технология разработки программ"
    assert bot._short_lesson_type("ЛЕКЦИЯ") == "лек."
    assert bot._short_lesson_type("ЛЕКЦИЯ/ПРАКТИКА") == "лек.+пр."
    assert bot._nice_audience("225") == "Ауд. 225"
    assert bot._nice_audience("СПОРТЗАЛ") == "Спортзал"

    text = bot.schedule_text("ИС 9-13", "в", "ВТОРНИК")
    assert "1. <b>10:00</b> · пр. — Архитектура аппаратных средств" in text
    assert "   Ауд. 225 · А.В. Дубенчук" in text
    assert "В этот день занятий нет." in bot.schedule_text("ИС 9-13", "в", "ВОСКРЕСЕНЬЕ")
    print("1. БД и форматирование расписания — ok")


def test_02_schedule_click_through() -> None:
    db.register_user(TEST_TG_ID, group_name="ИС 9-13")

    # открыть расписание -> заголовок дня и недели
    cb = FakeCallback(bot.MENU_SCHEDULE)
    run(bot.on_menu_schedule(cb))
    assert cb.message.edited_text is not None
    assert "🗓" in cb.message.edited_text

    # нажать кнопку дня -> текст редактируется, меняется день
    cb = FakeCallback(f"{bot.MENU_DAY}:в:ВТОРНИК")
    run(bot.on_menu_day(cb))
    assert cb.message.edited_text is not None
    assert "ВТОРНИК" in cb.message.edited_text
    assert "ВЕРХНЯЯ" in cb.message.edited_text

    # переключить неделю -> ВЕРХНЯЯ -> НИЖНЯЯ
    cb = FakeCallback(f"{bot.MENU_TOGGLE}:в:ВТОРНИК")
    run(bot.on_menu_toggle(cb))
    assert cb.message.edited_text is not None
    assert "НИЖНЯЯ" in cb.message.edited_text
    print("2. Кнопки панели реально правят текст — ok")


def test_03_truncated_callback_data() -> None:
    # обрезанные данные не должны ронять обработчик и не должны трогать сообщение
    cb = FakeCallback(f"{bot.MENU_DAY}:в")
    run(bot.on_menu_day(cb))
    assert cb.message.edited_text is None
    assert cb.toasts, "должен быть ответ кнопке"

    cb = FakeCallback(f"{bot.MENU_TOGGLE}:в")
    run(bot.on_menu_toggle(cb))
    assert cb.message.edited_text is None
    assert cb.toasts
    print("3. Обрезанный callback_data обрабатывается тихо — ok")


def test_04_unknown_callback() -> None:
    cb = FakeCallback("bogus:data")
    run(bot.on_unknown_callback(cb))
    assert cb.toasts
    print("4. Неизвестный callback не роняет бота — ok")


def test_05_registration_flow() -> None:
    cb = FakeCallback(f"{bot.GROUP_PREFIX}ИС 9-13", uid=TEST_TG_ID2)
    run(bot.on_group_selected(cb))
    assert "Подтверждение регистрации" in cb.message.edited_text

    cb = FakeCallback(f"{bot.CONFIRM_PREFIX}ИС 9-13", uid=TEST_TG_ID2)
    run(bot.on_confirm_registration(cb))
    user = db.get_user(TEST_TG_ID2)
    assert user is not None and user.group_name == "ИС 9-13"
    assert cb.message.deleted
    assert cb.message.answered
    print("5. Регистрация: выбор группы -> подтверждение — ok")


if __name__ == "__main__":
    try:
        test_01_db_and_formatters()
        test_02_schedule_click_through()
        test_03_truncated_callback_data()
        test_04_unknown_callback()
        test_05_registration_flow()
        print("\nВсе проверки прошли успешно.")
    finally:
        cleanup_user(TEST_TG_ID)
        cleanup_user(TEST_TG_ID2)