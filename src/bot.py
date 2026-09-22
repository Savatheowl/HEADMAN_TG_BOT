import asyncio
import logging
import sys
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import Config  # noqa: E402
from src.database import Database  # noqa: E402
from src.downloader import run_downloader  # noqa: E402
from src.parser import parse_schedule_files  # noqa: E402

logger = logging.getLogger(__name__)

db = Database()
router = Router()


class ActionLogMiddleware(BaseMiddleware):
    """Обновляет last_seen_at и пишет действия в статистику."""

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is not None:
            telegram_id = user.id
            db.touch_user(telegram_id)
            if isinstance(event, Message):
                db.log_action(telegram_id, "message", (event.text or "")[:255])
            elif isinstance(event, CallbackQuery):
                db.log_action(telegram_id, "callback", (event.data or "")[:255])
        return await handler(event, data)


BOT_INTRO = (
    "👋 Это бот с расписанием занятий.\n"
    "Здесь ты сможешь смотреть актуальное расписание своей группы "
    "и не пропускать изменения.\n\n"
    "Для начала выбери свой курс:"
)

COURSE_PREFIX = "course:"
GROUP_PREFIX = "group:"
CONFIRM_PREFIX = "confirm:"
CANCEL_PREFIX = "cancel:"
BACK_TO_COURSES = "back:courses"

NO_SCHEDULE_TEXT = "⚠️ В базе пока нет расписания. Попробуйте позже."

WEEK_EMOJI = {"в": "⬛", "н": "⬜"}
WEEK_LABELS = {"в": "ВЕРХНЯЯ", "н": "НИЖНЯЯ"}

DAY_SHORT = {
    "ПОНЕДЕЛЬНИК": "ПН",
    "ВТОРНИК": "ВТ",
    "СРЕДА": "СР",
    "ЧЕТВЕРГ": "ЧТ",
    "ПЯТНИЦА": "ПТ",
    "СУББОТА": "СБ",
}
DAY_NAMES = [
    "ПОНЕДЕЛЬНИК",
    "ВТОРНИК",
    "СРЕДА",
    "ЧЕТВЕРГ",
    "ПЯТНИЦА",
    "СУББОТА",
    "ВОСКРЕСЕНЬЕ",
]
WEEK_DAYS = list(DAY_SHORT)

MENU_SCHEDULE = "menu:schedule"
MENU_DAY = "menu:day"
MENU_TOGGLE = "menu:toggle"
MENU_HOME = "menu:home"


def courses_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for course in db.list_courses():
        builder.button(text=f"{course} курс", callback_data=f"{COURSE_PREFIX}{course}")
    builder.adjust(2)
    return builder.as_markup()


def groups_keyboard(course: int) -> InlineKeyboardMarkup | None:
    groups = db.list_groups_by_course(course)
    if not groups:
        return None
    builder = InlineKeyboardBuilder()
    for group in groups:
        builder.button(text=group, callback_data=f"{GROUP_PREFIX}{group}")
    builder.button(text="← Назад", callback_data=BACK_TO_COURSES)
    builder.adjust(2)
    return builder.as_markup()


def confirm_keyboard(course: int, group: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Подтвердить", callback_data=f"{CONFIRM_PREFIX}{group}")
    builder.button(text="❌ Отмена", callback_data=f"{CANCEL_PREFIX}{course}")
    builder.button(text="← Назад", callback_data=BACK_TO_COURSES)
    builder.adjust(1, 2)
    return builder.as_markup()


async def edit_group_menu(callback: CallbackQuery, course: int, title: str) -> None:
    keyboard = groups_keyboard(course)
    if keyboard is None:
        await callback.message.edit_text(
            f"😕 Для курса {course} пока нет расписания.\n\n"
            "Выбери другой курс:",
            reply_markup=courses_keyboard(),
        )
        return
    await callback.message.edit_text(title, reply_markup=keyboard)


def week_label(week: str) -> str:
    """Представление недели: эмодзи-квадрат + название (⬛ ВЕРХНЯЯ)."""
    return f"{WEEK_EMOJI.get(week, '')} {WEEK_LABELS.get(week, week)}".strip()


def _view_reference_date(now: datetime | None = None) -> datetime:
    """Опорная дата для панели по умолчанию.

    В воскресенье панель показывает ближайший понедельник следующей недели,
    поэтому опорной берётся следующий день (понедельник). В ПН–СБ — сегодня.
    """
    now = now or datetime.now()
    return now + timedelta(days=1) if now.weekday() == 6 else now


def current_week(now: datetime | None = None) -> str:
    """Текущая неделя: чётная ISO-неделя года = верхняя ('в'), нечётная = нижняя ('н').

    В воскресенье — неделя следующего понедельника.
    """
    iso_week = _view_reference_date(now).isocalendar()[1]
    week = "в" if iso_week % 2 == 0 else "н"
    if Config().week_parity_flip():
        week = "н" if week == "в" else "в"
    return week


def current_day(now: datetime | None = None) -> str:
    """Имя дня для панели по умолчанию (ПОНЕДЕЛЬНИК…ВОСКРЕСЕНЬЕ).

    В воскресенье возвращается ПОНЕДЕЛЬНИК следующей недели.
    """
    return DAY_NAMES[_view_reference_date(now).weekday()]


def main_menu_text(group: str, registered: bool = False) -> str:
    if registered:
        return f"✅ Регистрация завершена!\nГруппа: <b>{escape(group)}</b>"
    return f"📋 <b>Главное меню</b>\nГруппа: <b>{escape(group)}</b>"


def main_menu_markup() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Расписание", callback_data=MENU_SCHEDULE)
    builder.adjust(1)
    return builder.as_markup()


def _nice_discipline(raw: str) -> str:
    """Название дисциплины для показа: заглавная первая буква, остальное — с малой.

    Слова с точками/цифрами (аббревиатуры типа «МДК.02.01») сохраняются как есть.
    """
    words = raw.split()
    first_capitalized = False
    result = []
    for word in words:
        if word.replace(".", "").replace("-", "").replace("/", "").replace("(", "").replace(")", "").isalpha():
            result.append(word.capitalize() if not first_capitalized else word.lower())
            first_capitalized = True
        else:
            result.append(word)
    return " ".join(result)


def _nice_teacher(raw: str) -> str:
    """Преподаватель «СОЛОВЬЕВА Ф.А.» → «Ф.А. Соловьева».

    Непохожие на «ФАМИЛИЯ И.О…» строки возвращаются как есть.
    """
    tokens = [t for t in raw.strip().split() if t]
    if len(tokens) >= 2 and "." not in tokens[0]:
        surname, initials = tokens[0], tokens[1:]
        if all(("." in part and part.replace(".", "").isalpha() and bool(part.replace(".", ""))) for part in initials):
            surname = surname.capitalize() if surname.isupper() else surname
            return f"{' '.join(initials)} {surname}"
    return raw.strip()


def _short_lesson_type(raw: str) -> str:
    """Краткое обозначение типа занятия."""
    return {
        "ЛЕКЦИЯ": "лек.",
        "ПРАКТИКА": "пр.",
        "ЛЕКЦИЯ/ПРАКТИКА": "лек.+пр.",
    }.get(raw, raw.lower())


def _nice_audience(raw: str) -> str:
    """Аудитория: «225» → «Ауд. 225», «СПОРТЗАЛ» → «Спортзал»."""
    if not raw:
        return ""
    if raw.isdigit():
        return f"Ауд. {raw}"
    return raw.capitalize()


def schedule_text(group: str, week: str, day: str) -> str:
    lessons = db.get_lessons_for_group(group, week, day)
    lines = [
        f"🗓 <b>{day}</b> · {week_label(week)} неделя\n"
        f"Группа: <b>{escape(group)}</b>"
    ]
    if not lessons:
        lines.append("\nВ этот день занятий нет.")
        return "\n".join(lines)

    for i, lesson in enumerate(lessons, start=1):
        parts = [
            f"<b>{escape(lesson['time'])}</b>",
        ]
        if lesson["lesson_type"]:
            parts.append(_short_lesson_type(lesson["lesson_type"]))
        title = f"{i}. {' · '.join(parts)} — {escape(_nice_discipline(lesson['discipline']))}"
        meta = " · ".join(
            filter(
                None,
                [
                    escape(_nice_audience(lesson["audience"])),
                    escape(_nice_teacher(lesson["teacher"])) if lesson["teacher"] else "",
                ],
            )
        )
        if meta:
            lines.append(f"{title}\n   {meta}")
        else:
            lines.append(title)
    return "\n".join(lines)


def schedule_markup(week: str, day: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"{WEEK_EMOJI[week]} {WEEK_LABELS[week]}",
        callback_data=f"{MENU_TOGGLE}:{week}:{day}",
    )
    for day_name in WEEK_DAYS:
        marker = "• " if day_name == day else ""
        builder.button(
            text=f"{marker}{DAY_SHORT[day_name]}",
            callback_data=f"{MENU_DAY}:{week}:{day_name}",
        )
    builder.button(text="🏠 В главное меню", callback_data=MENU_HOME)
    builder.adjust(1, 3, 3, 1)
    return builder.as_markup()


async def apply_schedule_view(
    callback: CallbackQuery, group: str, week: str, day: str
) -> None:
    toast = f"{DAY_SHORT.get(day, day)} · {week_label(week)}"
    try:
        await callback.message.edit_text(
            schedule_text(group, week, day), reply_markup=schedule_markup(week, day)
        )
    except TelegramBadRequest as exc:
        if "not modified" in str(exc):
            logger.info(
                "Панель %s/%s/%s не изменена (%s)", group, week, day, exc
            )
            await callback.answer(f"{toast} — уже открыто")
            return
        logger.warning("Не удалось отредактировать расписание: %s", exc)
    await callback.answer(toast)


def _user_group(callback: CallbackQuery) -> str | None:
    user = db.get_user(callback.from_user.id)
    if user is None or not user.group_name:
        return None
    return user.group_name


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    telegram_id = message.from_user.id
    user = db.get_user(telegram_id)

    if user is not None and user.group_name:
        await message.answer(
            main_menu_text(user.group_name), reply_markup=main_menu_markup()
        )
        return

    if not db.list_courses():
        await message.answer(NO_SCHEDULE_TEXT)
        return

    await message.answer(BOT_INTRO, reply_markup=courses_keyboard())


@router.callback_query(F.data.startswith(COURSE_PREFIX))
async def on_course_selected(callback: CallbackQuery) -> None:
    try:
        course = int(callback.data.removeprefix(COURSE_PREFIX))
    except ValueError:
        await callback.answer("Неверный курс", show_alert=True)
        return

    await edit_group_menu(
        callback, course, f"📚 <b>Курс {course}</b>\nВыбери свою группу:"
    )
    await callback.answer(f"Курс {course}")


@router.callback_query(F.data.startswith(GROUP_PREFIX))
async def on_group_selected(callback: CallbackQuery) -> None:
    group = callback.data.removeprefix(GROUP_PREFIX)
    course = db.get_course_by_group(group)
    if course is None:
        await callback.answer("Группа не найдена", show_alert=True)
        return

    await callback.message.edit_text(
        "✍️ <b>Подтверждение регистрации</b>\n\n"
        f"Группа: <b>{escape(group)}</b>\n"
        f"Курс: {course}\n\n"
        "Продолжая регистрацию, ты подтверждаешь, что учишься в этой группе, "
        "и соглашаешься на обработку персональных данных.",
        reply_markup=confirm_keyboard(course, group),
    )
    await callback.answer()


@router.callback_query(F.data.startswith(CONFIRM_PREFIX))
async def on_confirm_registration(callback: CallbackQuery) -> None:
    telegram_id = callback.from_user.id
    group = callback.data.removeprefix(CONFIRM_PREFIX)

    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass

    db.register_user(
        telegram_id,
        username=callback.from_user.username,
        first_name=callback.from_user.first_name,
        last_name=callback.from_user.last_name,
        language_code=callback.from_user.language_code,
        group_name=group,
    )
    db.log_action(telegram_id, "register", group)
    logger.info("Регистрация завершена: %s -> группа %s", telegram_id, group)

    await callback.message.answer(
        main_menu_text(group, registered=True),
        reply_markup=main_menu_markup(),
    )


@router.callback_query(F.data.startswith(CANCEL_PREFIX))
async def on_cancel_confirmation(callback: CallbackQuery) -> None:
    try:
        course = int(callback.data.removeprefix(CANCEL_PREFIX))
    except ValueError:
        course = None

    if course is not None:
        await edit_group_menu(
            callback, course, f"📚 <b>Курс {course}</b>\nВыбери свою группу:"
        )
        await callback.answer()
        return

    await callback.message.edit_text(BOT_INTRO, reply_markup=courses_keyboard())
    await callback.answer()


@router.callback_query(F.data == BACK_TO_COURSES)
async def on_back_to_courses(callback: CallbackQuery) -> None:
    await callback.message.edit_text(BOT_INTRO, reply_markup=courses_keyboard())
    await callback.answer()


@router.callback_query(F.data == MENU_SCHEDULE)
async def on_menu_schedule(callback: CallbackQuery) -> None:
    group = _user_group(callback)
    if group is None:
        await callback.answer("Сначала выбери группу", show_alert=True)
        return
    await apply_schedule_view(callback, group, current_week(), current_day())


@router.callback_query(F.data.startswith(f"{MENU_DAY}:"))
async def on_menu_day(callback: CallbackQuery) -> None:
    group = _user_group(callback)
    if group is None:
        await callback.answer("Сначала выбери группу", show_alert=True)
        return
    try:
        _, _, week, day = callback.data.split(":")
    except ValueError:
        await callback.answer()
        return
    if week not in WEEK_LABELS or day not in DAY_SHORT:
        await callback.answer()
        return
    await apply_schedule_view(callback, group, week, day)


@router.callback_query(F.data.startswith(f"{MENU_TOGGLE}:"))
async def on_menu_toggle(callback: CallbackQuery) -> None:
    group = _user_group(callback)
    if group is None:
        await callback.answer("Сначала выбери группу", show_alert=True)
        return
    try:
        _, _, week, day = callback.data.split(":")
    except ValueError:
        await callback.answer()
        return
    other_week = "н" if week == "в" else "в"
    await apply_schedule_view(callback, group, other_week, day)


@router.callback_query(F.data == MENU_HOME)
async def on_menu_home(callback: CallbackQuery) -> None:
    group = _user_group(callback)
    if group is None:
        await callback.message.edit_text(BOT_INTRO, reply_markup=courses_keyboard())
        await callback.answer()
        return
    await callback.message.edit_text(
        main_menu_text(group), reply_markup=main_menu_markup()
    )
    await callback.answer()


@router.callback_query()
async def on_unknown_callback(callback: CallbackQuery) -> None:
    """Catch-all: любой нераспознанный/устаревший callback получает ответ."""
    logger.info("Неизвестный callback %r от %s", callback.data, callback.from_user.id)
    await callback.answer("Эта кнопка устарела, нажми /start", show_alert=False)


async def update_schedule_once() -> None:
    """Проверяет расписание в VK, скачивает новые файлы и обновляет БД.

    Сначала downloader сравнивает хэши и качает только изменившиеся файлы,
    затем parser перерабатывает только те версии, которые ещё не были распарсены.
    """
    config = Config()
    dest_dir = config.download_dir()
    try:
        report = await run_downloader(dest_dir=dest_dir)
        results = report["results"]
        saved = [r for r in results if r.get("status") == "saved"]
        logger.info(
            "Проверка расписания завершена: скачано новых %d, пропущено %d",
            len(saved), len(results) - len(saved),
        )

        parse_report = await asyncio.to_thread(parse_schedule_files, dest_dir)
        parsed = [name for name, item in parse_report.items() if item["status"] == "parsed"]
        logger.info("Расписание обновлено: перепарсено %d файлов", len(parsed))
    except Exception as exc:
        logger.error("Сбой проверки расписания: %s", exc)


async def schedule_updates_loop() -> None:
    """Фоновая задача: раз в UPDATE_INTERVAL_MINUTES проверяет и обновляет расписание."""
    interval_seconds = max(1, Config().update_interval_minutes()) * 60
    logger.info(
        "Фоновая проверка расписания запущена (каждые %d мин)", interval_seconds // 60
    )
    while True:
        await update_schedule_once()
        await asyncio.sleep(interval_seconds)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config = Config()
    token = config.bot_token()
    if not token or "ВАШ_ТОКЕН" in token:
        logger.error(
            "В .env не задан реальный BOT_TOKEN — бот не запущен. "
            "Вставьте токен от @BotFather."
        )
        return

    db.init_db()

    asyncio.create_task(schedule_updates_loop())

    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()

    dp.message.middleware(ActionLogMiddleware())
    dp.callback_query.middleware(ActionLogMiddleware())
    dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())