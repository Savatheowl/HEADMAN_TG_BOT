import asyncio
import logging
import sys
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import Config  # noqa: E402
from src.database import Database, UserRole, parse_course_from_filename  # noqa: E402
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
    "👋 <b>Привет! Это бот с расписанием занятий.</b>\n\n"
    "Здесь ты можешь смотреть актуальное расписание своей группы, "
    "не пропускать изменения и подписываться на обновления.\n\n"
    "Чтобы начать, выбери свой курс 👇"
)

COURSE_PREFIX = "course:"
GROUP_PREFIX = "group:"
CONFIRM_PREFIX = "confirm:"
CANCEL_PREFIX = "cancel:"
BACK_TO_COURSES = "back:courses"

SG_BACK = "sgback:courses"
SG_COURSE_PREFIX = "sgcourse:"
SG_GROUP_PREFIX = "sggroup:"
SG_CONFIRM_PREFIX = "sgconfirm:"
SG_CANCEL_PREFIX = "sgcancel:"

V_BACK = "vback:courses"
V_COURSE_PREFIX = "vcourse:"
V_GROUP_PREFIX = "vgroup:"

NO_SCHEDULE_TEXT = "⚠️ Расписания пока нет в базе. Загляни позже — подгружу его автоматически."


def no_schedule_for_course(course: int) -> str:
    return f"😕 Расписания для {course}-го курса пока нет.\n\nВыбери другой курс 👇"

NOTIFY_COURSE_UPDATE = (
    "📢 <b>Расписание {course}-го курса обновилось!</b>\n\n"
    "Проверь, не изменилось ли что-то у твоей группы: "
    "нажми /start и открой «🌞 Сегодня».{groups_part}"
)

HELP_TEXT = (
    "ℹ️ <b>Что умеет этот бот</b>\n\n"
    "• <b>🌞 Сегодня</b> и <b>▶️ Завтра</b> — расписание на день;\n"
    "• <b>📅 Неделя</b> — расписание на всю неделю;\n"
    "• <b>👀 Чужое расписание</b> — расписание другой группы;\n"
    "• <b>📄 PDF расписания</b> — файл-оригинал своего курса;\n"
    "• <b>📢 Объявления</b> — у старост и преподавателей;\n"
    "• <b>⚙️ Настройки</b> — сменить группу или сообщить о проблеме.\n\n"
    "Расписание обновляется автоматически, а при изменениях я пришлю уведомление.\n\n"
    "<b>Команды:</b>\n"
    "/start — открыть главное меню\n"
    "/help — эта помощь"
)

DESCRIPTION_TEXT = (
    "Это бот с расписанием занятий колледжа. В нём ты можешь смотреть расписание "
    "своей группы на день и на неделю, переключать верхнюю и нижнюю неделю, "
    "смотреть расписание других групп и скачивать PDF-версию расписания. "
    "Когда расписание обновляется, бот присылает уведомление.\n\n"
    "Если найдёшь проблему или захочешь предложить идею — напиши через "
    "«📮 Сообщить о проблеме / предложении» в настройках или командой /start."
)

SHORT_DESCRIPTION_TEXT = (
    "Расписание занятий твоей группы: на день, на неделю, PDF "
    "и уведомления об изменениях."
)

ABOUT_TEXT = (
    "Бот с расписанием занятий колледжа: день, неделя, другие группы, "
    "PDF и уведомления о новом расписании."
)

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
MENU_TODAY = "menu:today"
MENU_TOMORROW = "menu:tomorrow"
MENU_DAY = "menu:day"
MENU_TOGGLE = "menu:toggle"
MENU_WEEK = "menu:week"
MENU_HOME = "menu:home"
MENU_SETTINGS = "menu:settings"
MENU_FOREIGN = "menu:foreign"
MENU_PDF = "menu:pdf"
MENU_ANNOUNCE = "menu:announce"
MENU_ADMIN = "menu:admin"

SETTINGS_GROUP = "settings:group"

ADMIN_STATS = "admin:stats"
ADMIN_BROADCAST = "admin:broadcast"
ADMIN_ROLES = "admin:roles"
ADMIN_UPDATE = "admin:update"
ADMIN_BACK = "admin:back"

ADMIN_ROLE_PICK_PREFIX = "admin:role:pick:"
ADMIN_ROLE_SET_PREFIX = "admin:role:set:"

BROADCAST_AUD_PREFIX = "admin:bcast:aud:"
BROADCAST_CONFIRM = "admin:bcast:ok"
BROADCAST_REDO = "admin:bcast:redo"
BROADCAST_CANCEL = "admin:bcast:cancel"

ANNOUNCE_AUD_PREFIX = "announce:aud:"
ANNOUNCE_CONFIRM = "announce:ok"
ANNOUNCE_REDO = "announce:redo"
ANNOUNCE_CANCEL = "announce:cancel"

MENU_REPORT = "menu:report"
REPORT_BAN_PREFIX = "report:ban:"


class BroadcastStates(StatesGroup):
    """FSM ввода текста рассылки админа."""

    text = State()


class ReportStates(StatesGroup):
    """FSM ввода текста сообщения о проблеме/предложении."""

    text = State()


class AnnounceStates(StatesGroup):
    """FSM ввода текста объявления старосты."""

    text = State()


class RoleStates(StatesGroup):
    """FSM поиска пользователя для выдачи роли."""

    query = State()


def courses_keyboard(
    prefix: str = COURSE_PREFIX, back: str | None = None
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for course in db.list_courses():
        builder.button(text=f"{course} курс", callback_data=f"{prefix}{course}")
    if back:
        builder.button(text="← Назад", callback_data=back)
    builder.adjust(2)
    return builder.as_markup()


def groups_keyboard(
    course: int, prefix: str = GROUP_PREFIX, back: str = BACK_TO_COURSES
) -> InlineKeyboardMarkup | None:
    groups = db.list_groups_by_course(course)
    if not groups:
        return None
    builder = InlineKeyboardBuilder()
    for group in groups:
        builder.button(text=group, callback_data=f"{prefix}{group}")
    builder.button(text="← Назад", callback_data=back)
    builder.adjust(2)
    return builder.as_markup()


def confirm_keyboard(course: int, group: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Подтвердить", callback_data=f"{CONFIRM_PREFIX}{group}")
    builder.button(text="❌ Отмена", callback_data=f"{CANCEL_PREFIX}{course}")
    builder.button(text="← Назад", callback_data=BACK_TO_COURSES)
    builder.adjust(1, 2)
    return builder.as_markup()


def sg_confirm_keyboard(course: int, group: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Изменить", callback_data=f"{SG_CONFIRM_PREFIX}{group}")
    builder.button(text="❌ Отмена", callback_data=f"{SG_CANCEL_PREFIX}{course}")
    builder.button(text="← Назад", callback_data=SG_BACK)
    builder.adjust(1, 2)
    return builder.as_markup()


async def edit_group_menu(callback: CallbackQuery, course: int, title: str) -> None:
    keyboard = groups_keyboard(course)
    if keyboard is None:
        await callback.message.edit_text(
            no_schedule_for_course(course),
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


def view_day(now: datetime | None = None, days_offset: int = 0) -> tuple[str, str]:
    """(week, day) для панели на дату now + days_offset.

    Воскресенье (включая воскресенье, получившееся сдвигом) предстаёт
    понедельником следующей недели — так сб + 1 день ведёт сразу на ПН,
    а вс + 1 день — на ПН (как и само воскресенье).
    """
    target = (now or datetime.now()) + timedelta(days=days_offset)
    if target.weekday() == 6:
        target += timedelta(days=1)
    return week_for_date(target), DAY_NAMES[target.weekday()]


def week_for_date(target: datetime) -> str:
    """Неделя целевой даты: чётная ISO-неделя года = верхняя, нечётная = нижняя."""
    iso_week = target.isocalendar()[1]
    week = "в" if iso_week % 2 == 0 else "н"
    if Config().week_parity_flip():
        week = "н" if week == "в" else "в"
    return week


def current_week(now: datetime | None = None) -> str:
    """Текущая неделя: чётная ISO-неделя года = верхняя ('в'), нечётная = нижняя ('н').

    В воскресенье — неделя следующего понедельника.
    """
    return view_day(now, 0)[0]


def current_day(now: datetime | None = None) -> str:
    """Имя дня для панели по умолчанию (ПОНЕДЕЛЬНИК…ВОСКРЕСЕНЬЕ).

    В воскресенье возвращается ПОНЕДЕЛЬНИК следующей недели.
    """
    return view_day(now, 0)[1]


def main_menu_text(group: str, registered: bool = False) -> str:
    if registered:
        return f"✅ Регистрация завершена!\nГруппа: <b>{escape(group)}</b>"
    return f"📋 <b>Главное меню</b>\nГруппа: <b>{escape(group)}</b>"


def main_menu_markup(
    announcer: bool = False, is_admin: bool = False
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🌞 Сегодня", callback_data=MENU_TODAY)
    builder.button(text="▶️ Завтра", callback_data=MENU_TOMORROW)
    builder.button(text="📅 Неделя", callback_data=MENU_WEEK)
    builder.button(text="👀 Чужое расписание", callback_data=MENU_FOREIGN)
    builder.button(text="📄 PDF расписания", callback_data=MENU_PDF)
    builder.button(text="⚙️ Настройки", callback_data=MENU_SETTINGS)
    widths = [2, 2, 2]
    if announcer:
        builder.button(text="📢 Объявление", callback_data=MENU_ANNOUNCE)
        widths.append(1)
    if is_admin:
        builder.button(text="⚙️ Админ-панель", callback_data=MENU_ADMIN)
        widths.append(1)
    builder.adjust(*widths)
    return builder.as_markup()


def main_menu_for_user(telegram_id: int) -> InlineKeyboardMarkup:
    """Главное меню с учётом ролей пользователя."""
    return main_menu_markup(
        announcer=is_announcer(telegram_id), is_admin=is_admin(telegram_id)
    )


def admin_markup() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📊 Статистика", callback_data=ADMIN_STATS)
    builder.button(text="📣 Рассылка", callback_data=ADMIN_BROADCAST)
    builder.button(text="👥 Роли", callback_data=ADMIN_ROLES)
    builder.button(text="🔄 Обновить расписание", callback_data=ADMIN_UPDATE)
    builder.button(text="🏠 В главное меню", callback_data=MENU_HOME)
    builder.adjust(2, 2, 1)
    return builder.as_markup()


def stats_markup() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="← Назад", callback_data=ADMIN_BACK)
    builder.button(text="🏠 В главное меню", callback_data=MENU_HOME)
    builder.adjust(2)
    return builder.as_markup()


def admin_text() -> str:
    return "⚙️ <b>Админ-панель</b>\nУправление ботом и пользователями."


def settings_markup(show_report: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⚙️ Изменение группы", callback_data=SETTINGS_GROUP)
    if show_report:
        builder.button(text="📮 Сообщить о проблеме / предложении", callback_data=MENU_REPORT)
    builder.button(text="← Назад", callback_data=MENU_HOME)
    widths = [1, 1] if show_report else [1]
    builder.adjust(*widths)
    return builder.as_markup()


def _report_allowed(telegram_id: int) -> bool:
    """Может ли пользователь отправлять сообщения о проблеме/предложении."""
    if db.is_report_banned(telegram_id):
        return False
    return Config().report_admin_id() is not None


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


def schedule_markup(week: str, day: str, group: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"{WEEK_EMOJI[week]} {WEEK_LABELS[week]}",
        callback_data=f"{MENU_TOGGLE}:{week}:{day}:{group}",
    )
    for day_name in WEEK_DAYS:
        marker = "• " if day_name == day else ""
        builder.button(
            text=f"{marker}{DAY_SHORT[day_name]}",
            callback_data=f"{MENU_DAY}:{week}:{day_name}:{group}",
        )
    builder.button(text="📅 Неделя", callback_data=f"{MENU_WEEK}:{week}:{group}")
    builder.button(text="← Назад", callback_data=MENU_HOME)
    builder.adjust(1, 3, 3, 1, 1)
    return builder.as_markup()


def week_text(group: str, week: str) -> str:
    """Расписание на всю неделю компактно (без преподавателей и аудиторий)."""
    lines = [
        f"📅 <b>Неделя</b> · {week_label(week)}\n"
        f"Группа: <b>{escape(group)}</b>"
    ]
    for day in WEEK_DAYS:
        lessons = db.get_lessons_for_group(group, week, day)
        lines.append(f"\n<b>{day}</b> ({DAY_SHORT[day]}):")
        if not lessons:
            lines.append("   Нет занятий.")
            continue
        for i, lesson in enumerate(lessons, start=1):
            parts = [f"<b>{escape(lesson['time'])}</b>"]
            if lesson["lesson_type"]:
                parts.append(_short_lesson_type(lesson["lesson_type"]))
            lines.append(
                f"   {i}. {' · '.join(parts)} — {escape(_nice_discipline(lesson['discipline']))}"
            )
    text = "\n".join(lines)
    if len(text) > 3600:
        text = text[:3600].rstrip() + "\n\n… (показано не всё)"
    return text


def week_markup(week: str, group: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    other_week = "н" if week == "в" else "в"
    builder.button(
        text=f"Показать {week_label(other_week)}",
        callback_data=f"{MENU_WEEK}:{other_week}:{group}",
    )
    for day_name in WEEK_DAYS:
        builder.button(
            text=DAY_SHORT[day_name],
            callback_data=f"{MENU_DAY}:{week}:{day_name}:{group}",
        )
    builder.button(text="← Назад", callback_data=MENU_HOME)
    builder.adjust(1, 3, 3, 1)
    return builder.as_markup()


async def apply_schedule_view(
    callback: CallbackQuery, group: str, week: str, day: str
) -> None:
    toast = f"{DAY_SHORT.get(day, day)} · {week_label(week)}"
    try:
        await callback.message.edit_text(
            schedule_text(group, week, day),
            reply_markup=schedule_markup(week, day, group),
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


def user_role_level(telegram_id: int) -> int:
    """Числовой уровень роли: SUPERUSER > ADMIN > TEACHER/TRUSTED > STUDENT."""
    if telegram_id in Config().admin_ids():
        return UserRole.level(UserRole.SUPERUSER)
    user = db.get_user(telegram_id)
    if user is None:
        return -1
    return UserRole.level(user.role)


def is_admin(telegram_id: int) -> bool:
    return user_role_level(telegram_id) >= UserRole.level(UserRole.ADMIN)


def is_announcer(telegram_id: int) -> bool:
    """Староста/преподаватель: может слать объявления."""
    return user_role_level(telegram_id) >= UserRole.level(UserRole.TEACHER)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext | None = None) -> None:
    if state is not None:
        await state.clear()
    telegram_id = message.from_user.id
    user = db.get_user(telegram_id)

    if user is not None and user.group_name:
        await message.answer(
            main_menu_text(user.group_name), reply_markup=main_menu_for_user(telegram_id)
        )
        return

    if not db.list_courses():
        await message.answer(NO_SCHEDULE_TEXT)
        return

    await message.answer(BOT_INTRO, reply_markup=courses_keyboard())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.callback_query(F.data.startswith(COURSE_PREFIX))
async def on_course_selected(callback: CallbackQuery) -> None:
    try:
        course = int(callback.data.removeprefix(COURSE_PREFIX))
    except ValueError:
        await callback.answer("Неверный курс", show_alert=True)
        return

    await edit_group_menu(
        callback, course, f"📚 <b>Курс {course}</b>\nВыбери свою группу 👇"
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
        reply_markup=main_menu_for_user(telegram_id),
    )


@router.callback_query(F.data.startswith(CANCEL_PREFIX))
async def on_cancel_confirmation(callback: CallbackQuery) -> None:
    try:
        course = int(callback.data.removeprefix(CANCEL_PREFIX))
    except ValueError:
        course = None

    if course is not None:
        await edit_group_menu(
            callback, course, f"📚 <b>Курс {course}</b>\nВыбери свою группу 👇"
        )
        await callback.answer()
        return

    await callback.message.edit_text(BOT_INTRO, reply_markup=courses_keyboard())
    await callback.answer()


@router.callback_query(F.data == BACK_TO_COURSES)
async def on_back_to_courses(callback: CallbackQuery) -> None:
    await callback.message.edit_text(BOT_INTRO, reply_markup=courses_keyboard())
    await callback.answer()


@router.callback_query(F.data.in_({MENU_SCHEDULE, MENU_TODAY}))
async def on_menu_schedule(callback: CallbackQuery) -> None:
    """Кнопка «Сегодня» (и легаси «Расписание»): панель на текущую дату."""
    group = _user_group(callback)
    if group is None:
        await callback.answer("Сначала выбери группу", show_alert=True)
        return
    await apply_schedule_view(callback, group, *view_day())


@router.callback_query(F.data == MENU_TOMORROW)
async def on_menu_tomorrow(callback: CallbackQuery) -> None:
    group = _user_group(callback)
    if group is None:
        await callback.answer("Сначала выбери группу", show_alert=True)
        return
    await apply_schedule_view(callback, group, *view_day(days_offset=1))


def _panel_group_from_data(payload: str) -> tuple[str, str, str | None]:
    """Разбирает payload панели на (week, day, group|None).

    Формат: '{week}:{day}' — своя группа (легаси), '{week}:{day}:{group}' — чужая.
    """
    parts = payload.split(":")
    if len(parts) < 2:
        return ("", "", None)
    week, day = parts[0], parts[1]
    if week not in WEEK_LABELS or day not in DAY_SHORT:
        return ("", "", None)
    group = ":".join(parts[2:]) if len(parts) > 2 else None
    return (week, day, group or None)


@router.callback_query(F.data.startswith(f"{MENU_DAY}:"))
async def on_menu_day(callback: CallbackQuery) -> None:
    week, day, group = _panel_group_from_data(
        callback.data.removeprefix(f"{MENU_DAY}:")
    )
    if not week:
        await callback.answer()
        return
    if group is None:
        group = _user_group(callback)
        if group is None:
            await callback.answer("Сначала выбери группу", show_alert=True)
            return
    await apply_schedule_view(callback, group, week, day)


@router.callback_query(F.data.startswith(f"{MENU_TOGGLE}:"))
async def on_menu_toggle(callback: CallbackQuery) -> None:
    week, day, group = _panel_group_from_data(
        callback.data.removeprefix(f"{MENU_TOGGLE}:")
    )
    if not week:
        await callback.answer()
        return
    if group is None:
        group = _user_group(callback)
        if group is None:
            await callback.answer("Сначала выбери группу", show_alert=True)
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
        main_menu_text(group), reply_markup=main_menu_for_user(callback.from_user.id)
    )
    await callback.answer()


@router.callback_query(F.data == MENU_SETTINGS)
async def on_menu_settings(callback: CallbackQuery) -> None:
    group = _user_group(callback)
    header = "⚙️ <b>Настройки</b>"
    if group:
        header += f"\nГруппа: <b>{escape(group)}</b>"
    await callback.message.edit_text(
        header, reply_markup=settings_markup(_report_allowed(callback.from_user.id))
    )
    await callback.answer()


@router.callback_query(F.data == SETTINGS_GROUP)
async def on_settings_change_group(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "📚 <b>Выбери курс</b>\nСмена группы — выбери свой курс 👇",
        reply_markup=courses_keyboard(prefix=SG_COURSE_PREFIX, back=MENU_SETTINGS),
    )
    await callback.answer()


@router.callback_query(F.data == SG_BACK)
async def on_sg_back(callback: CallbackQuery) -> None:
    await on_settings_change_group(callback)


@router.callback_query(F.data.startswith(SG_COURSE_PREFIX))
async def on_sg_course(callback: CallbackQuery) -> None:
    try:
        course = int(callback.data.removeprefix(SG_COURSE_PREFIX))
    except ValueError:
        await callback.answer("Неверный курс", show_alert=True)
        return

    keyboard = groups_keyboard(course, prefix=SG_GROUP_PREFIX, back=SG_BACK)
    if keyboard is None:
        await callback.message.edit_text(
            f"😕 Расписания для {course}-го курса пока нет.\n\nВыбери другой курс 👇",
            reply_markup=courses_keyboard(prefix=SG_COURSE_PREFIX, back=MENU_SETTINGS),
        )
    else:
        await callback.message.edit_text(
            f"📚 <b>Курс {course}</b>\nВыбери свою группу 👇", reply_markup=keyboard
        )
    await callback.answer(f"Курс {course}")


@router.callback_query(F.data.startswith(SG_GROUP_PREFIX))
async def on_sg_group(callback: CallbackQuery) -> None:
    group = callback.data.removeprefix(SG_GROUP_PREFIX)
    course = db.get_course_by_group(group)
    if course is None:
        await callback.answer("Группа не найдена", show_alert=True)
        return

    await callback.message.edit_text(
        "✍️ <b>Изменение группы</b>\n\n"
        f"Группа: <b>{escape(group)}</b>\n"
        f"Курс: {course}\n\n"
        "Подтверди смену группы:",
        reply_markup=sg_confirm_keyboard(course, group),
    )
    await callback.answer()


@router.callback_query(F.data.startswith(SG_CONFIRM_PREFIX))
async def on_sg_confirm(callback: CallbackQuery) -> None:
    telegram_id = callback.from_user.id
    group = callback.data.removeprefix(SG_CONFIRM_PREFIX)

    user = db.update_user_group(telegram_id, group)
    if user is None:
        await callback.answer("Ты ещё не зарегистрирован. Нажми /start", show_alert=True)
        return
    db.log_action(telegram_id, "change_group", group)
    logger.info("Группа изменена: %s -> %s", telegram_id, group)

    await callback.message.edit_text(
        main_menu_text(group), reply_markup=main_menu_for_user(telegram_id)
    )
    await callback.answer("Группа изменена")


@router.callback_query(F.data.startswith(SG_CANCEL_PREFIX))
async def on_sg_cancel(callback: CallbackQuery) -> None:
    try:
        course = int(callback.data.removeprefix(SG_CANCEL_PREFIX))
    except ValueError:
        course = None

    if course is not None:
        keyboard = groups_keyboard(course, prefix=SG_GROUP_PREFIX, back=SG_BACK)
        if keyboard is None:
            await callback.answer("Групп для курса нет", show_alert=True)
            return
        await callback.message.edit_text(
            f"📚 <b>Курс {course}</b>\nВыбери свою группу 👇", reply_markup=keyboard
        )
        await callback.answer()
        return

    await callback.message.edit_text(
        "⚙️ <b>Настройки</b>",
        reply_markup=settings_markup(_report_allowed(callback.from_user.id)),
    )
    await callback.answer()


def report_ban_markup(telegram_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🚫 Забанить автора",
        callback_data=f"{REPORT_BAN_PREFIX}{telegram_id}",
    )
    return builder.as_markup()


@router.callback_query(F.data == MENU_REPORT)
async def on_menu_report(callback: CallbackQuery, state: FSMContext) -> None:
    if db.is_report_banned(callback.from_user.id):
        await callback.answer(
            "Ты не можешь отправлять сообщения о проблеме.", show_alert=True
        )
        return
    if Config().report_admin_id() is None:
        await callback.answer("Отправка временно недоступна.", show_alert=True)
        return
    await state.set_state(ReportStates.text)
    await callback.message.edit_text(
        "📮 <b>Сообщение о проблеме / предложении</b>\n\n"
        "Напиши одним сообщением, что случилось или что предложить. "
        "Текст уйдёт главному админу.\n\nДля отмены — /start."
    )
    await callback.answer()


@router.message(ReportStates.text)
async def on_report_text(message: Message, state: FSMContext) -> None:
    await state.clear()
    if db.is_report_banned(message.from_user.id):
        await message.answer("Ты не можешь отправлять сообщения о проблеме.")
        return
    admin_id = Config().report_admin_id()
    if admin_id is None:
        await message.answer("Отправка временно недоступна.")
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer(
            "Сообщение не может быть пустым — напиши текст ещё раз. Для отмены — /start."
        )
        return
    text = escape(text)

    user = db.get_user(message.from_user.id)
    first = message.from_user.first_name or (user.first_name if user else None)
    last = message.from_user.last_name or (user.last_name if user else None)
    username = message.from_user.username or (user.username if user else None)
    full_name = " ".join(part for part in (first, last) if part) or "Без имени"

    sender = f"От: <b>{escape(full_name)}</b>"
    if username:
        sender += f" (@{escape(username)})"
    lines = ["📮 <b>Сообщение о проблеме / предложении</b>", "", sender]
    if user and user.group_name:
        lines.append(f"Группа: <b>{escape(user.group_name)}</b>")
    lines.extend(
        [
            f"TG id: {message.from_user.id}",
            f"Время: {datetime.now().strftime('%d.%m.%Y %H:%M')}",
            "",
            text,
        ]
    )

    db.log_action(message.from_user.id, "report")
    await message.bot.send_message(
        admin_id,
        "\n".join(lines),
        reply_markup=report_ban_markup(message.from_user.id),
    )
    await message.answer("✅ Сообщение отправлено. Спасибо за обратную связь!")


@router.callback_query(F.data.startswith(REPORT_BAN_PREFIX))
async def on_report_ban(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    raw = callback.data.removeprefix(REPORT_BAN_PREFIX)
    try:
        reporter_id = int(raw)
    except ValueError:
        await callback.answer("Неверные данные", show_alert=True)
        return
    db.ban_report_user(reporter_id)
    db.log_action(reporter_id, "report_ban", str(callback.from_user.id))
    new_text = (
        f"{callback.message.text}\n\n🚫 Автор забанен — новые "
        "сообщения о проблеме от него не принимаются."
    )
    await callback.message.edit_text(new_text)
    await callback.answer("Автор забанен")


@router.callback_query(F.data == MENU_FOREIGN)
async def on_menu_foreign(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "👀 <b>Чужое расписание</b>\nВыбери курс:",
        reply_markup=courses_keyboard(prefix=V_COURSE_PREFIX, back=MENU_HOME),
    )
    await callback.answer()


@router.callback_query(F.data == V_BACK)
async def on_v_back(callback: CallbackQuery) -> None:
    await on_menu_foreign(callback)


@router.callback_query(F.data.startswith(V_COURSE_PREFIX))
async def on_v_course(callback: CallbackQuery) -> None:
    try:
        course = int(callback.data.removeprefix(V_COURSE_PREFIX))
    except ValueError:
        await callback.answer("Неверный курс", show_alert=True)
        return

    keyboard = groups_keyboard(course, prefix=V_GROUP_PREFIX, back=V_BACK)
    if keyboard is None:
        await callback.message.edit_text(
            f"😕 Расписания для {course}-го курса пока нет.\n\nВыбери другой курс 👇",
            reply_markup=courses_keyboard(prefix=V_COURSE_PREFIX, back=MENU_HOME),
        )
    else:
        await callback.message.edit_text(
            f"📚 <b>Курс {course}</b>\nВыбери свою группу 👇", reply_markup=keyboard
        )
    await callback.answer(f"Курс {course}")


@router.callback_query(F.data.startswith(V_GROUP_PREFIX))
async def on_v_group(callback: CallbackQuery) -> None:
    group = callback.data.removeprefix(V_GROUP_PREFIX)
    course = db.get_course_by_group(group)
    if course is None:
        await callback.answer("Группа не найдена", show_alert=True)
        return

    await apply_schedule_view(callback, group, *view_day())
    db.log_action(callback.from_user.id, "view_foreign", group)


async def _show_week(callback: CallbackQuery, group: str, week: str) -> None:
    try:
        await callback.message.edit_text(
            week_text(group, week), reply_markup=week_markup(week, group)
        )
    except TelegramBadRequest as exc:
        if "not modified" in str(exc):
            await callback.answer(f"{week_label(week)} — уже открыто")
            return
        logger.warning("Не удалось показать неделю: %s", exc)
    await callback.answer(f"{week_label(week)} неделя")


@router.callback_query(F.data == MENU_WEEK)
async def on_menu_week_now(callback: CallbackQuery) -> None:
    """«📅 Неделя» из главного меню: текущая неделя своей группы."""
    group = _user_group(callback)
    if group is None:
        await callback.answer("Сначала выбери группу", show_alert=True)
        return
    week, _ = view_day()
    await _show_week(callback, group, week)


@router.callback_query(F.data.startswith(f"{MENU_WEEK}:"))
async def on_menu_week(callback: CallbackQuery) -> None:
    payload = callback.data.removeprefix(f"{MENU_WEEK}:")
    parts = payload.split(":", 1)
    week = parts[0]
    group = parts[1] if len(parts) > 1 else None
    if week not in WEEK_LABELS:
        await callback.answer()
        return
    if not group:
        group = _user_group(callback)
        if group is None:
            await callback.answer("Сначала выбери группу", show_alert=True)
            return
    await _show_week(callback, group, week)


def _pdf_document(course: int) -> FSInputFile | None:
    record = db.get_file_for_course(course)
    if record is None:
        return None
    path = Path(Config().download_dir()) / record.name
    if not path.exists():
        logger.warning("Файл расписания %s отсутствует на диске", path)
        return None
    return FSInputFile(path)


@router.callback_query(F.data == MENU_PDF)
async def on_menu_pdf(callback: CallbackQuery) -> None:
    group = _user_group(callback)
    if group is None:
        await callback.answer("Сначала выбери группу", show_alert=True)
        return
    course = db.get_course_by_group(group)
    if course is None:
        await callback.answer("Курс не найден", show_alert=True)
        return
    document = _pdf_document(course)
    if document is None:
        await callback.answer("PDF ещё не готов — попробуй позже", show_alert=True)
        return
    await callback.answer("Отправляю файл…")
    await callback.bot.send_document(
        chat_id=callback.from_user.id,
        document=document,
        caption=f"📄 Расписание {course}-го курса",
    )
    db.log_action(callback.from_user.id, "pdf", str(course))


def _target_label(target: int | str | None) -> str:
    if target is None:
        return "все пользователи"
    if isinstance(target, int):
        return f"{target} курс"
    return target


def _broadcast_audience_markup() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="👥 Все пользователи", callback_data=f"{BROADCAST_AUD_PREFIX}all"
    )
    for course in db.list_courses():
        builder.button(
            text=f"{course} курс", callback_data=f"{BROADCAST_AUD_PREFIX}course:{course}"
        )
    builder.button(text="← Назад", callback_data=ADMIN_BACK)
    builder.adjust(2, 1)
    return builder.as_markup()


def _confirm_markup(ok_data: str, redo_data: str, cancel_data: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Отправить", callback_data=ok_data)
    builder.button(text="🔄 Заново", callback_data=redo_data)
    builder.button(text="❌ Отмена", callback_data=cancel_data)
    builder.adjust(1, 2)
    return builder.as_markup()


@router.callback_query(F.data == MENU_ADMIN)
async def on_menu_admin(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа к админ-панели", show_alert=True)
        return
    await callback.message.edit_text(admin_text(), reply_markup=admin_markup())
    await callback.answer()


@router.callback_query(F.data == ADMIN_BACK)
async def on_admin_back(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(admin_text(), reply_markup=admin_markup())
    await callback.answer()


@router.callback_query(F.data == ADMIN_STATS)
async def on_admin_stats(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    courses = db.list_courses()
    per_course = db.users_by_course_counts()
    actions = db.action_stats(days=30)
    lines = ["📊 <b>Статистика</b>", ""]
    lines.append(f"Всего пользователей: {db.count_total_users()}")
    lines.append(f"Активных за 30 дней: {db.count_active_users()}")
    lines.append("По курсам:")
    for course in courses:
        lines.append(f"  • {course} курс: {per_course.get(course, 0)}")
    lines.append("Действия за 30 дней:")
    if actions:
        for action, count in sorted(actions.items(), key=lambda kv: -kv[1]):
            lines.append(f"  • {action}: {count}")
    else:
        lines.append("  пока нет")
    await callback.message.edit_text("\n".join(lines), reply_markup=stats_markup())
    await callback.answer()


@router.callback_query(F.data == ADMIN_BROADCAST)
async def on_admin_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.message.edit_text(
        "📣 <b>Рассылка</b>\nКому отправить?",
        reply_markup=_broadcast_audience_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith(BROADCAST_AUD_PREFIX))
async def on_broadcast_audience(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    raw = callback.data.removeprefix(BROADCAST_AUD_PREFIX)
    if raw == "all":
        target = None
    elif raw.startswith("course:"):
        try:
            target = int(raw.split(":")[1])
        except ValueError:
            await callback.answer("Неверный курс", show_alert=True)
            return
    else:
        target = None
    await state.set_state(BroadcastStates.text)
    await state.update_data(target=target, origin="admin")
    await callback.message.edit_text(
        f"📣 Аудитория: <b>{_target_label(target)}</b>\n\n"
        "Отправь текст сообщения. Для отмены — /start.",
    )
    await callback.answer()


@router.message(BroadcastStates.text)
async def on_broadcast_text(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        await message.answer("Нет доступа")
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("Текст не может быть пустым — пришли сообщение ещё раз. Для отмены — /start.")
        return
    text = escape(text)
    await state.update_data(text=text)
    data = await state.get_data()
    await message.answer(
        f"📣 <b>Подтверждение рассылки</b>\n\n"
        f"Аудитория: <b>{_target_label(data.get('target'))}</b>\n\n"
        f"{text}\n\nОтправить?",
        reply_markup=_confirm_markup(
            BROADCAST_CONFIRM, BROADCAST_REDO, BROADCAST_CANCEL
        ),
    )


@router.callback_query(F.data == BROADCAST_CONFIRM)
async def on_broadcast_confirm(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    data = await state.get_data()
    target = data.get("target")
    text = data.get("text") or ""
    await state.clear()
    users = db.list_users_for_broadcast(target)
    await callback.message.edit_text(
        f"📣 Рассылка запущена: {len(users)} получателей…"
    )
    sent = await send_to_users(bot, users, text)
    db.log_action(
        callback.from_user.id, "broadcast", f"{_target_label(target)}: {sent}/{len(users)}"
    )
    await callback.message.edit_text(f"✅ Отправлено {sent} из {len(users)}")
    await callback.answer("Расслылка отправлена")


@router.callback_query(F.data == BROADCAST_REDO)
async def on_broadcast_redo(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    data = await state.get_data()
    target = data.get("target")
    await state.set_state(BroadcastStates.text)
    await callback.message.edit_text(
        f"📣 Аудитория: <b>{_target_label(target)}</b>\n\nОтправь текст ещё раз:"
    )
    await callback.answer()


@router.callback_query(F.data == BROADCAST_CANCEL)
async def on_broadcast_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text(admin_text(), reply_markup=admin_markup())
    await callback.answer("Отменено")


@router.callback_query(F.data == ADMIN_ROLES)
async def on_admin_roles(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(RoleStates.query)
    await callback.message.edit_text(
        "👥 <b>Роли</b>\nВведи telegram id, @username или имя — найду пользователя:",
    )
    await callback.answer()


@router.message(RoleStates.query)
async def on_roles_query(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        await message.answer("Нет доступа")
        return
    query = (message.text or "").strip()
    if not query:
        await message.answer("Пустой запрос — введи что-нибудь или нажми /start.")
        return
    users = db.search_users(query, limit=30)
    if not users:
        await message.answer("Никого не нашёл. Попробуй другой запрос или /start.")
        return
    builder = InlineKeyboardBuilder()
    for user in users:
        label = f"{user.telegram_id} · {user.first_name or ''} {user.last_name or ''}".strip()
        if user.group_name:
            label += f" · {user.group_name}"
        builder.button(text=label, callback_data=f"{ADMIN_ROLE_PICK_PREFIX}{user.telegram_id}")
    builder.button(text="← Назад", callback_data=ADMIN_BACK)
    builder.adjust(1)
    await message.answer("Найденные пользователи:", reply_markup=builder.as_markup())


@router.callback_query(F.data.startswith(ADMIN_ROLE_PICK_PREFIX))
async def on_admin_role_pick(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    try:
        telegram_id = int(callback.data.removeprefix(ADMIN_ROLE_PICK_PREFIX))
    except ValueError:
        await callback.answer("Некорректный id", show_alert=True)
        return
    user = db.get_user(telegram_id)
    if user is None:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    await state.set_state(RoleStates.query)
    await state.update_data(role_user_id=telegram_id)
    builder = InlineKeyboardBuilder()
    for role in (UserRole.STUDENT, UserRole.TEACHER, UserRole.TRUSTED, UserRole.ADMIN, UserRole.SUPERUSER):
        builder.button(
            text=UserRole.label(role),
            callback_data=f"{ADMIN_ROLE_SET_PREFIX}{telegram_id}:{role}",
        )
    builder.button(text="← Назад", callback_data=ADMIN_ROLES)
    builder.adjust(2)
    name = f"{user.first_name or ''} {user.last_name or ''}".strip() or str(telegram_id)
    await callback.message.edit_text(
        f"👥 <b>Роль</b>\nПользователь: <b>{escape(name)}</b>\n"
        f"Текущая роль: {UserRole.label(user.role)}\n\nВыбери новую:",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith(ADMIN_ROLE_SET_PREFIX))
async def on_admin_role_set(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    payload = callback.data.removeprefix(ADMIN_ROLE_SET_PREFIX)
    telegram_id_str, role = payload.split(":", 1)
    try:
        telegram_id = int(telegram_id_str)
    except ValueError:
        await callback.answer("Некорректный id", show_alert=True)
        return
    result = db.set_role(telegram_id, role)
    await state.clear()
    if not result:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    db.log_action(callback.from_user.id, "set_role", f"{telegram_id}:{role}")
    await callback.message.edit_text(
        f"✅ Роль пользователя {telegram_id}: <b>{UserRole.label(role)}</b>",
        reply_markup=admin_markup(),
    )
    await callback.answer("Роль обновлена")


@router.callback_query(F.data == ADMIN_UPDATE)
async def on_admin_update(callback: CallbackQuery, bot: Bot) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    asyncio.create_task(update_schedule_once(bot))
    await callback.answer("Обновление расписания запущено")


def _announce_audience_markup(user_course: int | None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if user_course is not None:
        builder.button(
            text=f"Мой курс ({user_course})",
            callback_data=f"{ANNOUNCE_AUD_PREFIX}course:{user_course}",
        )
    builder.button(
        text="Все курсы", callback_data=f"{ANNOUNCE_AUD_PREFIX}all"
    )
    builder.button(text="← Назад", callback_data=MENU_HOME)
    builder.adjust(1)
    return builder.as_markup()


@router.callback_query(F.data == MENU_ANNOUNCE)
async def on_menu_announce(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_announcer(callback.from_user.id):
        await callback.answer("Нет доступа к объявлениям", show_alert=True)
        return
    user_course = None
    group = _user_group(callback)
    if group is not None:
        user_course = db.get_course_by_group(group)
    await callback.message.edit_text(
        "📢 <b>Объявление</b>\nКуда отправить?",
        reply_markup=_announce_audience_markup(user_course),
    )
    await callback.answer()


@router.callback_query(F.data.startswith(ANNOUNCE_AUD_PREFIX))
async def on_announce_audience(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_announcer(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    raw = callback.data.removeprefix(ANNOUNCE_AUD_PREFIX)
    if raw.startswith("course:"):
        try:
            target = int(raw.split(":")[1])
        except ValueError:
            await callback.answer("Неверный курс", show_alert=True)
            return
    else:
        target = None
    await state.set_state(AnnounceStates.text)
    await state.update_data(target=target, origin="announce")
    await callback.message.edit_text(
        f"📢 Аудитория: <b>{_target_label(target)}</b>\n\n"
        "Отправь текст объявления. Для отмены — /start.",
    )
    await callback.answer()


@router.message(AnnounceStates.text)
async def on_announce_text(message: Message, state: FSMContext) -> None:
    if not is_announcer(message.from_user.id):
        await state.clear()
        await message.answer("Нет доступа")
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("Текст не может быть пустым — пришли сообщение ещё раз. Для отмены — /start.")
        return
    text = escape(text)
    await state.update_data(text=text)
    data = await state.get_data()
    await message.answer(
        f"📢 <b>Подтверждение объявления</b>\n\n"
        f"Аудитория: <b>{_target_label(data.get('target'))}</b>\n\n"
        f"{text}\n\nОтправить?",
        reply_markup=_confirm_markup(
            ANNOUNCE_CONFIRM, ANNOUNCE_REDO, ANNOUNCE_CANCEL
        ),
    )


@router.callback_query(F.data == ANNOUNCE_CONFIRM)
async def on_announce_confirm(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    if not is_announcer(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    data = await state.get_data()
    target = data.get("target")
    text = data.get("text") or ""
    await state.clear()
    users = db.list_users_for_broadcast(target)
    await callback.message.edit_text(
        f"📢 Объявление отправляется: {len(users)} получателей…"
    )
    sent = await send_to_users(bot, users, text)
    db.log_action(
        callback.from_user.id, "announce", f"{_target_label(target)}: {sent}/{len(users)}"
    )
    await callback.message.edit_text(f"✅ Отправлено {sent} из {len(users)}")
    await callback.answer("Объявление отправлено")


@router.callback_query(F.data == ANNOUNCE_REDO)
async def on_announce_redo(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_announcer(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    data = await state.get_data()
    target = data.get("target")
    await state.set_state(AnnounceStates.text)
    await callback.message.edit_text(
        f"📢 Аудитория: <b>{_target_label(target)}</b>\n\nОтправь текст ещё раз:"
    )
    await callback.answer()


@router.callback_query(F.data == ANNOUNCE_CANCEL)
async def on_announce_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text(
        "📋 <b>Главное меню</b>",
        reply_markup=main_menu_for_user(callback.from_user.id),
    )
    await callback.answer("Отменено")


@router.callback_query()
async def on_unknown_callback(callback: CallbackQuery) -> None:
    """Catch-all: любой нераспознанный/устаревший callback получает ответ."""
    logger.info("Неизвестный callback %r от %s", callback.data, callback.from_user.id)
    await callback.answer("Эта кнопка устарела, нажми /start", show_alert=False)


def _groups_part(groups: list[str] | None, limit: int = 6) -> str:
    """Строка «Группы с изменениями: …» или пустая, если списка нет."""
    if not groups:
        return ""
    shown = groups[:limit]
    tail = f" и ещё {len(groups) - limit}" if len(groups) > limit else ""
    return "\nГруппы с изменениями: " + ", ".join(shown) + tail


async def send_to_users(bot: Bot, users, text: str) -> int:
    """Отправляет text каждому пользователю из списка, возвращает число успехов."""
    sent = 0
    for user in users:
        try:
            await bot.send_message(user.telegram_id, text)
            sent += 1
        except (TelegramForbiddenError, TelegramNotFound) as exc:
            logger.info("Пользователь %s недоступен: %s", user.telegram_id, exc)
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 1)
            try:
                await bot.send_message(user.telegram_id, text)
                sent += 1
            except TelegramAPIError as retry_exc:
                logger.warning(
                    "Повторная отправка %s не удалась: %s", user.telegram_id, retry_exc
                )
        except TelegramAPIError as exc:
            logger.warning("Не удалось отправить %s: %s", user.telegram_id, exc)
        await asyncio.sleep(0.05)
    return sent


async def notify_course_update(
    bot: Bot, course: int, groups: list[str] | None = None
) -> int:
    """Уведомляет всех студентов курса о новой версии расписания.

    groups — список групп, в которых есть изменения (для умного уведомления).
    Возвращает количество успешно отправленных сообщений.
    """
    users = db.list_users_by_course(course)
    text = NOTIFY_COURSE_UPDATE.format(
        course=course, groups_part=_groups_part(sorted(groups) if groups else None)
    )
    sent = await send_to_users(bot, users, text)
    logger.info(
        "Уведомлено о новом расписании курса %d (%d групп): %d из %d",
        course, len(groups or []), sent, len(users),
    )
    return sent


async def update_schedule_once(bot: Bot | None = None) -> None:
    """Проверяет расписание в VK, скачивает новые файлы и обновляет БД.

    Сначала downloader сравнивает хэши и качает только изменившиеся файлы,
    затем parser перерабатывает только те версии, которые ещё не были распарсены.
    Если передан bot и нашлись реально изменившиеся курсы, студентам этих курсов
    рассылаются уведомления о новой версии расписания.
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

        if bot is not None and parsed:
            updates: dict[int, set[str]] = {}
            for name in parsed:
                course = parse_course_from_filename(name)
                if course is None:
                    continue
                record = db.get_file(name)
                if record is None:
                    continue
                updates.setdefault(course, set()).update(db.list_groups_for_file(record.id))
            for course in sorted(updates):
                await notify_course_update(bot, course, sorted(updates[course]))
    except Exception as exc:
        logger.error("Сбой проверки расписания: %s", exc)


async def schedule_updates_loop(bot: Bot) -> None:
    """Фоновая задача: раз в UPDATE_INTERVAL_MINUTES проверяет и обновляет расписание."""
    interval_seconds = max(1, Config().update_interval_minutes()) * 60
    logger.info(
        "Фоновая проверка расписания запущена (каждые %d мин)", interval_seconds // 60
    )
    while True:
        await update_schedule_once(bot)
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

    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()

    try:
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Запустить бота и открыть главное меню"),
                BotCommand(command="help", description="Помощь и список команд"),
            ]
        )
        await bot.set_my_description(DESCRIPTION_TEXT)
        await bot.set_my_short_description(SHORT_DESCRIPTION_TEXT)
    except Exception as exc:
        logger.warning("Не удалось зарегистрировать команды и описание бота: %s", exc)

    asyncio.create_task(schedule_updates_loop(bot))

    dp.message.middleware(ActionLogMiddleware())
    dp.callback_query.middleware(ActionLogMiddleware())
    dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())