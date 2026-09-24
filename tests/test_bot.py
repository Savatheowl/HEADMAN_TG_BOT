"""Интеграционные проверки для HEADMAN_TG_BOT.

Запуск из корня репозитория:
    .venv/bin/python tests/test_bot.py

Тест использует реальный файл БД бота, но регистрирует отдельного
тестового пользователя и удаляет его в конце.
"""

import asyncio
from pathlib import Path
import os
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import logging
logging.disable(logging.CRITICAL)

import bot
import downloader
from config import Config
from database import Database

TEST_TG_ID = 987_654_321_000
TEST_TG_ID2 = 987_654_321_001
TEST_TG_ID3 = 987_654_321_002
TEST_TG_ID4 = 987_654_321_003
TEST_TG_ID5 = 987_654_321_004

db = Database()
db.init_db()


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
        self.bot = None

    async def answer(self, *args, **kwargs) -> None:
        self.toasts.append((args, kwargs))


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.documents: list[tuple[int, object, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))

    async def send_document(self, chat_id: int, document, caption: str = "") -> None:
        self.documents.append((chat_id, document, caption))


class FakeChatMessage:
    """Обычное текстовое сообщение пользователя (для FSM-шагов)."""

    def __init__(self, text: str = "", uid: int = TEST_TG_ID) -> None:
        self.text = text
        self.from_user = FakeUser(uid)
        self.edited_text = None
        self.reply_markup = None
        self.answered = []

    async def edit_text(self, text: str, reply_markup=None) -> None:
        self.edited_text = text
        self.reply_markup = reply_markup

    async def answer(self, text: str = "", **kwargs) -> None:
        self.answered.append(text)
        if "reply_markup" in kwargs:
            self.reply_markup = kwargs["reply_markup"]


class FakeState:
    """Имитация FSMContext с хранением данных в памяти."""

    def __init__(self) -> None:
        self.data: dict = {}
        self.current = None

    async def set_state(self, state) -> None:
        self.current = state

    async def update_data(self, **kwargs) -> None:
        self.data.update(kwargs)

    async def get_data(self) -> dict:
        return dict(self.data)

    async def clear(self) -> None:
        self.data = {}


def cleanup_user(uid: int) -> None:
    from sqlalchemy import delete
    from database import BotUser
    from sqlalchemy.orm import Session
    with Session(db.engine) as session:
        session.execute(delete(BotUser).where(BotUser.telegram_id == uid))
        session.commit()


def cleanup_report_ban(uid: int) -> None:
    from sqlalchemy import delete
    from database import ReportBan
    from sqlalchemy.orm import Session
    with Session(db.engine) as session:
        session.execute(delete(ReportBan).where(ReportBan.telegram_id == uid))
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


def test_06_course_update_notifications() -> None:
    class FakeBot:
        def __init__(self) -> None:
            self.sent: list[tuple[int, str]] = []

        async def send_message(self, chat_id: int, text: str) -> None:
            self.sent.append((chat_id, text))

    group5 = db.list_groups_by_course(5)[0]
    db.register_user(TEST_TG_ID3, group_name="ИС 9-13")
    db.register_user(TEST_TG_ID4, group_name=group5)

    users_course4 = {u.telegram_id for u in db.list_users_by_course(4)}
    assert TEST_TG_ID3 in users_course4
    assert TEST_TG_ID4 not in users_course4

    fake_bot = FakeBot()
    run(bot.notify_course_update(fake_bot, 4))
    sent = {
        uid: text
        for uid, text in fake_bot.sent
        if uid in (TEST_TG_ID3, TEST_TG_ID4)
    }
    assert sent == {TEST_TG_ID3: bot.NOTIFY_COURSE_UPDATE.format(course=4, groups_part="")}
    print("6. Уведомление студентов курса о новой версии — ok")


def test_07_menu_and_view_day() -> None:
    from datetime import datetime

    markup = bot.main_menu_markup()
    texts = [btn.text for row in markup.inline_keyboard for btn in row]
    data = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert texts == ["🌞 Сегодня", "▶️ Завтра", "📅 Неделя", "👀 Чужое расписание", "📄 PDF расписания", "⚙️ Настройки"]
    assert data == [
        bot.MENU_TODAY, bot.MENU_TOMORROW, bot.MENU_WEEK, bot.MENU_FOREIGN,
        bot.MENU_PDF, bot.MENU_SETTINGS,
    ]

    announce_menu = bot.main_menu_markup(announcer=True)
    all_btn = [
        btn.text for row in announce_menu.inline_keyboard for btn in row
    ][-1]
    assert all_btn == "📢 Объявление"

    admin_menu = bot.main_menu_markup(announcer=True, is_admin=True)
    last_two = [
        btn.text for row in admin_menu.inline_keyboard for btn in row
    ][-2:]
    assert last_two == ["📢 Объявление", "⚙️ Админ-панель"]

    monday = datetime(2026, 9, 21, 12, 0)          # ПН
    week, day = bot.view_day(monday)
    assert (week, day) == (bot.view_day(monday, 0)[0], "ПОНЕДЕЛЬНИК")

    sunday = datetime(2026, 9, 27, 12, 0)          # ВС → ПН следующей недели
    week, day = bot.view_day(sunday)
    assert day == "ПОНЕДЕЛЬНИК"
    assert week == bot.view_day(datetime(2026, 9, 28, 12, 0))[0]

    saturday = datetime(2026, 9, 26, 12, 0)        # СБ + 1 день → ПН следующей недели
    week, day = bot.view_day(saturday, 1)
    assert day == "ПОНЕДЕЛЬНИК"
    assert week == bot.view_day(datetime(2026, 9, 28, 12, 0))[0]

    friday = datetime(2026, 9, 25, 12, 0)          # ПТ + 1 день → СБ, неделя та же
    week, day = bot.view_day(friday, 1)
    assert day == "СУББОТА"
    assert week == bot.view_day(friday, 0)[0]
    print("7. Главное меню и расчёт дней «Сегодня»/«Завтра» — ok")


def test_08_settings_change_group() -> None:
    group5 = db.list_groups_by_course(5)[0]
    db.register_user(TEST_TG_ID5, group_name="ИС 9-13")

    cb = FakeCallback(bot.MENU_SETTINGS, uid=TEST_TG_ID5)
    run(bot.on_menu_settings(cb))
    assert "Настройки" in cb.message.edited_text
    assert "Изменение группы" in " ".join(
        btn.text for row in cb.message.reply_markup.inline_keyboard for btn in row
    )

    cb = FakeCallback(bot.SETTINGS_GROUP, uid=TEST_TG_ID5)
    run(bot.on_settings_change_group(cb))
    assert "Выбери курс" in cb.message.edited_text

    cb = FakeCallback(f"{bot.SG_COURSE_PREFIX}5", uid=TEST_TG_ID5)
    run(bot.on_sg_course(cb))
    assert "Курс 5" in cb.message.edited_text

    cb = FakeCallback(bot.SG_BACK, uid=TEST_TG_ID5)
    run(bot.on_sg_back(cb))
    assert "Выбери курс" in cb.message.edited_text

    cb = FakeCallback(f"{bot.SG_COURSE_PREFIX}5", uid=TEST_TG_ID5)
    run(bot.on_sg_course(cb))
    cb = FakeCallback(f"{bot.SG_GROUP_PREFIX}{group5}", uid=TEST_TG_ID5)
    run(bot.on_sg_group(cb))
    assert "Изменение группы" in cb.message.edited_text
    assert group5 in cb.message.edited_text

    cb = FakeCallback(f"{bot.SG_CONFIRM_PREFIX}{group5}", uid=TEST_TG_ID5)
    run(bot.on_sg_confirm(cb))
    assert db.get_user(TEST_TG_ID5).group_name == group5
    assert "Главное меню" in cb.message.edited_text
    print("8. Настройки: смена группы с подтверждением — ok")


def test_09_foreign_schedule() -> None:
    group5 = db.list_groups_by_course(5)[0]
    db.register_user(TEST_TG_ID5, group_name=group5)

    cb = FakeCallback(bot.MENU_FOREIGN, uid=TEST_TG_ID5)
    run(bot.on_menu_foreign(cb))
    assert "Чужое расписание" in cb.message.edited_text

    cb = FakeCallback(f"{bot.V_COURSE_PREFIX}4", uid=TEST_TG_ID5)
    run(bot.on_v_course(cb))
    assert "Курс 4" in cb.message.edited_text

    cb = FakeCallback(f"{bot.V_GROUP_PREFIX}ИС 9-13", uid=TEST_TG_ID5)
    run(bot.on_v_group(cb))
    assert "🗓" in cb.message.edited_text
    assert "Группа: <b>ИС 9-13</b>" in cb.message.edited_text

    # навигация по дням и неделям остаётся на чужой группе, а не на своей (group5)
    cb = FakeCallback(f"{bot.MENU_DAY}:в:ВТОРНИК:ИС 9-13", uid=TEST_TG_ID5)
    run(bot.on_menu_day(cb))
    assert "ВТОРНИК" in cb.message.edited_text
    assert "Группа: <b>ИС 9-13</b>" in cb.message.edited_text
    assert f"Группа: <b>{group5}</b>" not in cb.message.edited_text

    cb = FakeCallback(f"{bot.MENU_TOGGLE}:в:ВТОРНИК:ИС 9-13", uid=TEST_TG_ID5)
    run(bot.on_menu_toggle(cb))
    assert "НИЖНЯЯ" in cb.message.edited_text
    assert "Группа: <b>ИС 9-13</b>" in cb.message.edited_text
    print("9. Чужое расписание: курс → группа, навигация на чужой группе — ok")


def test_10_smart_notification_groups() -> None:
    fake_bot = FakeBot()
    run(bot.notify_course_update(fake_bot, 4, groups=["ИС 9-11", "ИС 9-13"]))
    assert "Группы с изменениями: ИС 9-11, ИС 9-13" in fake_bot.sent[0][1]

    many = [f"ГР-{i}" for i in range(8)]
    part = bot._groups_part(many)
    assert "ГР-0, ГР-1, ГР-2, ГР-3, ГР-4, ГР-5" in part
    assert " и ещё 2" in part

    assert bot._groups_part(None) == ""
    assert bot._groups_part([]) == ""
    print("10. Умное уведомление: перечень групп с изменениями — ok")


def test_11_week_view() -> None:
    db.register_user(TEST_TG_ID, group_name="ИС 9-13")

    # «📅 Неделя» из главного меню: текущая неделя своей группы
    cb = FakeCallback(bot.MENU_WEEK)
    run(bot.on_menu_week_now(cb))
    assert "Неделя" in cb.message.edited_text
    assert "Группа: <b>ИС 9-13</b>" in cb.message.edited_text

    # по недельному виду: кнопки дней ведут в панель дня с группой
    current_week, _ = bot.view_day()
    cb = FakeCallback(f"{bot.MENU_WEEK}:{current_week}")
    run(bot.on_menu_week(cb))
    assert "Неделя" in cb.message.edited_text
    assert "ПОНЕДЕЛЬНИК" in cb.message.edited_text
    assert "Группа: <b>ИС 9-13</b>" in cb.message.edited_text

    markup = bot.week_markup(current_week, "ИС 9-13")
    btn_texts = [btn.text for row in markup.inline_keyboard for btn in row]
    assert btn_texts[0].startswith("Показать")
    assert btn_texts[-1] == "← Назад"

    # в панели дня тоже есть «← Назад»
    day_markup = bot.schedule_markup(current_week, "ПОНЕДЕЛЬНИК", "ИС 9-13")
    day_btns = [btn.text for row in day_markup.inline_keyboard for btn in row]
    assert "← Назад" in day_btns
    assert "🏠 В главное меню" not in day_btns
    print("11. Расписание на неделю и кнопка Назад — ok")


def test_12_pdf_download() -> None:
    db.register_user(TEST_TG_ID5, group_name="ИС 9-13")

    cb = FakeCallback(bot.MENU_PDF, uid=TEST_TG_ID5)
    cb.bot = FakeBot()
    run(bot.on_menu_pdf(cb))
    assert cb.bot.documents, "должен быть отправлен документ"
    assert "4-го курса" in cb.bot.documents[0][2]

    # без группы — alert, без отправки
    cb2 = FakeCallback(bot.MENU_PDF, uid=987_654_321_999)
    cb2.bot = FakeBot()
    run(bot.on_menu_pdf(cb2))
    assert not cb2.bot.documents
    assert any("Сначала выбери группу" in (args[0] if args else "") for args, _ in cb2.toasts)
    print("12. PDF расписания: отправка и гейт без группы — ok")


def test_13_menu_roles_and_gates() -> None:
    db.register_user(TEST_TG_ID4, group_name="ИС 9-13")
    db.set_role(TEST_TG_ID4, bot.UserRole.TRUSTED)
    db.register_user(TEST_TG_ID5, group_name="ИС 9-13")
    db.set_role(TEST_TG_ID5, bot.UserRole.ADMIN)

    student_btns = [
        btn.text for row in bot.main_menu_for_user(TEST_TG_ID).inline_keyboard for btn in row
    ]
    assert "📢 Объявление" not in student_btns
    assert "⚙️ Админ-панель" not in student_btns

    announcer_btns = [
        btn.text for row in bot.main_menu_for_user(TEST_TG_ID4).inline_keyboard for btn in row
    ]
    assert "📢 Объявление" in announcer_btns
    assert "⚙️ Админ-панель" not in announcer_btns

    admin_btns = [
        btn.text for row in bot.main_menu_for_user(TEST_TG_ID5).inline_keyboard for btn in row
    ]
    assert "📢 Объявление" in admin_btns
    assert "⚙️ Админ-панель" in admin_btns

    cb = FakeCallback(bot.MENU_ANNOUNCE, uid=TEST_TG_ID)
    run(bot.on_menu_announce(cb, FakeState()))
    assert any("Нет доступа" in (args[0] if args else "") for args, _ in cb.toasts)

    cb = FakeCallback(bot.MENU_ADMIN, uid=TEST_TG_ID)
    run(bot.on_menu_admin(cb))
    assert any("Нет доступа" in (args[0] if args else "") for args, _ in cb.toasts)
    print("13. Роли и гейты: меню и отказ в доступе — ok")


def test_14_admin_panel_stats() -> None:
    cb = FakeCallback(bot.MENU_ADMIN, uid=TEST_TG_ID5)
    run(bot.on_menu_admin(cb))
    assert "Админ-панель" in cb.message.edited_text

    cb = FakeCallback(bot.ADMIN_STATS, uid=TEST_TG_ID5)
    run(bot.on_admin_stats(cb))
    assert "Статистика" in cb.message.edited_text
    assert "Всего пользователей:" in cb.message.edited_text
    assert "По курсам:" in cb.message.edited_text

    cb = FakeCallback(bot.ADMIN_STATS, uid=TEST_TG_ID)
    run(bot.on_admin_stats(cb))
    assert any("Нет доступа" in (args[0] if args else "") for args, _ in cb.toasts)
    print("14. Админ-панель: статистика и гейт — ok")


def test_15_admin_broadcast() -> None:
    db.register_user(TEST_TG_ID, group_name="ИС 9-13")
    db.register_user(TEST_TG_ID3, group_name="ИС 9-13")
    state = FakeState()

    cb = FakeCallback(bot.ADMIN_BROADCAST, uid=TEST_TG_ID5)
    run(bot.on_admin_broadcast(cb, state))
    assert "Рассылка" in cb.message.edited_text

    cb = FakeCallback(f"{bot.BROADCAST_AUD_PREFIX}course:4", uid=TEST_TG_ID5)
    run(bot.on_broadcast_audience(cb, state))
    assert state.data["target"] == 4
    assert "Отправь текст сообщения" in cb.message.edited_text

    msg = FakeChatMessage("Завтра пары отменяются", uid=TEST_TG_ID5)
    run(bot.on_broadcast_text(msg, state))
    assert state.data["text"] == "Завтра пары отменяются"
    assert any("Подтверждение рассылки" in a for a in msg.answered)

    fake_bot = FakeBot()
    cb = FakeCallback(bot.BROADCAST_CONFIRM, uid=TEST_TG_ID5)
    run(bot.on_broadcast_confirm(cb, state, fake_bot))
    assert len(fake_bot.sent) >= 2
    assert all(text == "Завтра пары отменяются" for _, text in fake_bot.sent)
    assert state.data == {}
    print("15. Админ: рассылка (аудитория → текст → подтверждение) — ok")


def test_16_admin_roles_flow() -> None:
    db.register_user(TEST_TG_ID4, group_name="ИС 9-13")
    state = FakeState()

    cb = FakeCallback(bot.ADMIN_ROLES, uid=TEST_TG_ID5)
    run(bot.on_admin_roles(cb, state))

    msg = FakeChatMessage(str(TEST_TG_ID4), uid=TEST_TG_ID5)
    run(bot.on_roles_query(msg, state))
    assert any("Найденные пользователи" in a for a in msg.answered)
    pick_data = f"{bot.ADMIN_ROLE_PICK_PREFIX}{TEST_TG_ID4}"
    found = [
        btn.callback_data
        for row in msg.reply_markup.inline_keyboard for btn in row
    ]
    assert pick_data in found

    cb = FakeCallback(pick_data, uid=TEST_TG_ID5)
    run(bot.on_admin_role_pick(cb, state))
    assert "Выбери новую" in cb.message.edited_text

    cb = FakeCallback(
        f"{bot.ADMIN_ROLE_SET_PREFIX}{TEST_TG_ID4}:{bot.UserRole.TRUSTED}",
        uid=TEST_TG_ID5,
    )
    run(bot.on_admin_role_set(cb, state))
    assert db.get_user(TEST_TG_ID4).role == bot.UserRole.TRUSTED
    assert state.data == {}
    print("16. Админ: выдача роли через поиск пользователя — ok")


def test_17_announce_flow() -> None:
    db.register_user(TEST_TG_ID, group_name="ИС 9-13")
    state = FakeState()

    cb = FakeCallback(bot.MENU_ANNOUNCE, uid=TEST_TG_ID4)
    run(bot.on_menu_announce(cb, state))
    assert "Объявление" in cb.message.edited_text
    announce_btns = [
        btn.text for row in cb.message.reply_markup.inline_keyboard for btn in row
    ]
    assert "Мой курс (4)" in announce_btns

    cb = FakeCallback(f"{bot.ANNOUNCE_AUD_PREFIX}course:4", uid=TEST_TG_ID4)
    run(bot.on_announce_audience(cb, state))
    assert state.data["target"] == 4

    msg = FakeChatMessage("Собрание группы в 12:00", uid=TEST_TG_ID4)
    run(bot.on_announce_text(msg, state))
    assert any("Подтверждение объявления" in a for a in msg.answered)

    fake_bot = FakeBot()
    cb = FakeCallback(bot.ANNOUNCE_CONFIRM, uid=TEST_TG_ID4)
    run(bot.on_announce_confirm(cb, state, fake_bot))
    assert any(text == "Собрание группы в 12:00" for _, text in fake_bot.sent)
    assert state.data == {}
    print("17. Объявление старосты: курс → текст → подтверждение — ok")


def test_18_report_flow() -> None:
    """Сообщение о проблеме/предложении: отправка админу и бан автора."""

    admin = 987_654_321_099
    os.environ["REPORT_ADMIN_ID"] = str(admin)
    os.environ["ADMIN_IDS"] = str(admin)

    db.set_role(TEST_TG_ID5, bot.UserRole.ADMIN)
    db.register_user(TEST_TG_ID, group_name="ИС 9-13")

    def datas(markup) -> list:
        result = []
        for row in markup.inline_keyboard:
            for btn in row:
                result.append(btn.callback_data)
        return result

    cb = FakeCallback(bot.MENU_SETTINGS, uid=TEST_TG_ID)
    run(bot.on_menu_settings(cb))
    assert bot.MENU_REPORT in datas(cb.message.reply_markup)

    state = FakeState()
    cb = FakeCallback(bot.MENU_REPORT, uid=TEST_TG_ID)
    run(bot.on_menu_report(cb, state))
    assert state.current == bot.ReportStates.text
    assert "Сообщение о проблеме" in cb.message.edited_text

    class ReportBot(FakeBot):
        def __init__(self) -> None:
            super().__init__()
            self.sent_markups: dict = {}

        async def send_message(self, chat_id: int, text: str, reply_markup=None) -> None:
            self.sent.append((chat_id, text))
            self.sent_markups[chat_id] = reply_markup

    class ReportMessage(FakeChatMessage):
        def __init__(self, text: str, uid: int, bot) -> None:
            super().__init__(text, uid)
            self.bot = bot

    fake_bot = ReportBot()
    msg = ReportMessage("Календарь не подгружается", TEST_TG_ID, fake_bot)
    run(bot.on_report_text(msg, state))
    assert state.data == {}
    assert len(fake_bot.sent) == 1 and fake_bot.sent[0][0] == admin
    sent_text = fake_bot.sent[0][1]
    assert "Сообщение о проблеме / предложении" in sent_text
    assert "<b>Тест Тестов</b>" in sent_text
    assert "@test_user" in sent_text
    assert "ИС 9-13" in sent_text
    assert "Время: " in sent_text
    assert "Календарь не подгружается" in sent_text
    assert f"{bot.REPORT_BAN_PREFIX}{TEST_TG_ID}" in datas(fake_bot.sent_markups[admin])
    assert any("Сообщение отправлено" in a for a in msg.answered)

    cb = FakeCallback(f"{bot.REPORT_BAN_PREFIX}{TEST_TG_ID}", uid=TEST_TG_ID5)
    cb.message.text = sent_text
    run(bot.on_report_ban(cb))
    assert db.is_report_banned(TEST_TG_ID)
    assert "Автор забанен" in cb.message.edited_text

    cb = FakeCallback(bot.MENU_SETTINGS, uid=TEST_TG_ID)
    run(bot.on_menu_settings(cb))
    assert bot.MENU_REPORT not in datas(cb.message.reply_markup)

    cb = FakeCallback(bot.MENU_REPORT, uid=TEST_TG_ID)
    run(bot.on_menu_report(cb, state))
    assert cb.toasts

    msg = ReportMessage("Ещё одна жалоба", TEST_TG_ID, fake_bot)
    run(bot.on_report_text(msg, state))
    assert any("не можешь" in a for a in msg.answered)
    assert len(fake_bot.sent) == 1, "админу ничего не ушло после бана"

    os.environ["REPORT_ADMIN_ID"] = ""
    os.environ["ADMIN_IDS"] = ""
    cb = FakeCallback(bot.MENU_SETTINGS, uid=TEST_TG_ID2)
    run(bot.on_menu_settings(cb))
    assert bot.MENU_REPORT not in datas(cb.message.reply_markup)

    del os.environ["REPORT_ADMIN_ID"]
    del os.environ["ADMIN_IDS"]
    print("18. Сообщение о проблеме: отправка админу и бан автора — ok")


def test_19_help_command() -> None:
    msg = FakeChatMessage("/help", uid=TEST_TG_ID)
    run(bot.cmd_help(msg))
    assert msg.answered
    text = msg.answered[0]
    assert "Что умеет этот бот" in text
    assert "/start — открыть главное меню" in text
    assert "/help — эта помощь" in text
    assert bot.SHORT_DESCRIPTION_TEXT.startswith("Расписание занятий твоей группы")
    print("19. Команда /help: помощь и список команд — ok")


def test_20_downloader_retry() -> None:
    class PageStub:
        def __init__(self, failures: int):
            self.failures = failures
            self.calls = 0

        async def goto(self, url: str, **kwargs) -> None:
            self.calls += 1
            if self.calls <= self.failures:
                raise downloader.PlaywrightTimeoutError("таймаут")

    async def scenario(failures: int, attempts: int) -> dict:
        page = PageStub(failures)
        try:
            await downloader.goto_with_retry(
                page, "https://vk.ru/topic-1", attempts=attempts,
                delay_between=(0.0, 0.0),
            )
            ok = True
        except (downloader.PlaywrightTimeoutError, downloader.PlaywrightError):
            ok = False
        return {"ok": ok, "calls": page.calls}

    # Два падения — третья попытка успешна.
    res = run(scenario(failures=2, attempts=3))
    assert res["ok"] and res["calls"] == 3, res
    # Все попытки исчерпаны — последняя ошибка проброшена.
    res = run(scenario(failures=3, attempts=3))
    assert not res["ok"] and res["calls"] == 3, res
    print("20. Ретрай навигации скачивателя (провальные попытки → успех) — ok")


def test_21_downloader_proxy_config() -> None:
    assert downloader.proxy_config_from_url("") is None
    assert downloader.proxy_config_from_url("   ") is None
    assert downloader.proxy_config_from_url("socks5://1.2.3.4:1080") == {
        "server": "socks5://1.2.3.4:1080",
    }
    assert downloader.proxy_config_from_url("http://user:pwd@host:8080") == {
        "server": "http://user:pwd@host:8080",
    }
    # PROXY_URL пуст в .env / по умолчанию — прямое соединение.
    assert Config().proxy_url() == ""
    print("21. Формирование proxy-конфига из PROXY_URL — ok")


def test_22_downloader_host_fallback() -> None:
    class PageStub:
        def __init__(self, fail_urls: set[str]):
            self.fail_urls = fail_urls
            self.visited: list[str] = []

        async def goto(self, url: str, **kwargs) -> None:
            self.visited.append(url)
            if url in self.fail_urls:
                raise downloader.PlaywrightTimeoutError("таймаут")

        async def wait_for_selector(self, selector: str, **kwargs) -> None:
            return None

        async def eval_on_selector_all(self, selector: str, js: str) -> list[dict]:
            return [{"href": "/doc1", "text": "Расписание 1 курс"}]

    orig = downloader.goto_with_retry

    async def fake_goto(page, url, **kwargs) -> None:
        await page.goto(url)

    downloader.goto_with_retry = fake_goto
    try:
        async def scenario(fail_urls: set[str], urls: list[str]) -> dict:
            page = PageStub(fail_urls)
            try:
                links = await downloader.fetch_links_from_hosts(page, urls)
                return {"links": links, "visited": page.visited}
            except RuntimeError:
                return {"links": None, "visited": page.visited}

        # Аномальный (vk.ru) хост падает → фолбэк на vk.com.
        res = run(scenario({"https://vk.ru/topic-61699714_47589048"},
                           ["https://vk.ru/topic-61699714_47589048",
                            "https://vk.com/topic-61699714_47589048"]))
        assert res["visited"] == ["https://vk.ru/topic-61699714_47589048",
                                  "https://vk.com/topic-61699714_47589048"], res
        assert res["links"][0]["url"] == "https://vk.ru/doc1", res  # относительный href → VK_BASE
        # Все хосты падают → RuntimeError.
        res = run(scenario({"https://vk.ru/topic-61699714_47589048"},
                           ["https://vk.ru/topic-61699714_47589048"]))
        assert res["links"] is None and len(res["visited"]) == 1, res
        print("22. Фолбэк-хосты темы (vk.ru → vk.com) — ok")
    finally:
        downloader.goto_with_retry = orig


if __name__ == "__main__":
    try:
        test_01_db_and_formatters()
        test_02_schedule_click_through()
        test_03_truncated_callback_data()
        test_04_unknown_callback()
        test_05_registration_flow()
        test_06_course_update_notifications()
        test_07_menu_and_view_day()
        test_08_settings_change_group()
        test_09_foreign_schedule()
        test_10_smart_notification_groups()
        test_11_week_view()
        test_12_pdf_download()
        test_13_menu_roles_and_gates()
        test_14_admin_panel_stats()
        test_15_admin_broadcast()
        test_16_admin_roles_flow()
        test_17_announce_flow()
        test_18_report_flow()
        test_19_help_command()
        test_20_downloader_retry()
        test_21_downloader_proxy_config()
        test_22_downloader_host_fallback()
        print("\nВсе проверки прошли успешно.")
    finally:
        cleanup_report_ban(TEST_TG_ID)
        cleanup_user(TEST_TG_ID)
        cleanup_user(TEST_TG_ID2)
        cleanup_user(TEST_TG_ID3)
        cleanup_user(TEST_TG_ID4)
        cleanup_user(TEST_TG_ID5)