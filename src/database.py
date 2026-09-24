import hashlib
import logging
import re
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    create_engine,
    delete,
    event,
    func,
    inspect,
    or_,
    select,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import Config  # noqa: E402

logger = logging.getLogger("database")


class Base(DeclarativeBase):
    pass


class UserRole:
    """Роли пользователей (enum-коды хранятся в БД)."""

    STUDENT = "student"
    TEACHER = "teacher"
    TRUSTED = "trusted"
    ADMIN = "admin"
    SUPERUSER = "superuser"

    LABELS = {
        STUDENT: "Студент",
        TEACHER: "Преподаватель",
        TRUSTED: "Доверенный пользователь",
        ADMIN: "Администрация",
        SUPERUSER: "Суперпользователь",
    }

    LEVELS = {
        STUDENT: 0,
        TEACHER: 1,
        TRUSTED: 1,
        ADMIN: 2,
        SUPERUSER: 3,
    }

    @classmethod
    def label(cls, role: str) -> str:
        return cls.LABELS.get(role, role)

    @classmethod
    def level(cls, role: str) -> int:
        return cls.LEVELS.get(role, 0)


class FileVersion(Base):
    """Таблица контроля версий скачанных файлов."""

    __tablename__ = "file_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    parsed_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    source_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    lessons: Mapped[list["ScheduleLesson"]] = relationship(
        "ScheduleLesson", back_populates="file_version", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return (
            f"<FileVersion name={self.name!r} version={self.version} "
            f"size={self.size} hash={self.file_hash[:12]}...>"
        )


class ScheduleLesson(Base):
    """Одно занятие расписания. Связано с версией файла, из которой получено."""

    __tablename__ = "schedule_lessons"
    __table_args__ = (
        Index("ix_lessons_group_day", "group_name", "day_of_week", "time"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_version_id: Mapped[int] = mapped_column(
        ForeignKey("file_versions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    course: Mapped[int | None] = mapped_column(Integer, nullable=True)
    group_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    day_of_week: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    time: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    week: Mapped[str] = mapped_column(String(10), nullable=False, default="")
    discipline: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    audience: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    lesson_type: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    teacher: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    file_version = relationship("FileVersion", back_populates="lessons")

    def __repr__(self) -> str:
        return f"<ScheduleLesson {self.group_name} {self.day_of_week} {self.time} {self.discipline!r}>"


class BotUser(Base):
    """Зарегистрированный пользователь бота."""

    __tablename__ = "bot_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    language_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    group_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default=UserRole.STUDENT)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def __repr__(self) -> str:
        return f"<BotUser {self.telegram_id} role={self.role!r} group={self.group_name!r}>"


class UserAction(Base):
    """Действие пользователя для статистики."""

    __tablename__ = "user_actions"
    __table_args__ = (
        Index("ix_actions_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("bot_users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<UserAction {self.action} payload={self.payload!r}>"


class ReportBan(Base):
    """Бан на отправку сообщений о проблеме/предложении (ключ — telegram_id)."""

    __tablename__ = "report_bans"

    telegram_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, nullable=False
    )
    banned_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<ReportBan {self.telegram_id} since={self.banned_at!r}>"


@dataclass
class UserInfo:
    """Снапшот пользователя — безопасен для использования после закрытия сессии."""

    telegram_id: int
    id: int = 0
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    language_code: str | None = None
    group_name: str | None = None
    role: str = UserRole.STUDENT
    is_active: bool = True
    last_seen_at: datetime | None = None

    @classmethod
    def from_model(cls, user: "BotUser") -> "UserInfo":
        return cls(
            id=user.id,
            telegram_id=user.telegram_id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            language_code=user.language_code,
            group_name=user.group_name,
            role=user.role,
            is_active=user.is_active,
            last_seen_at=user.last_seen_at,
        )


COURSE_RE = re.compile(r"_(\d+)_KURS", re.IGNORECASE)


def parse_course_from_filename(name: str) -> int | None:
    """Возвращает номер курса из имени файла расписания.

    Пример: '1_POLUGODIE_3_KURS_2026-2027.pdf' -> 3.
    Для файлов без 'KURS' (например, звонки) возвращает None.
    """
    match = COURSE_RE.search(name)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


class Database:
    """Базовый доступ к БД SQLite через SQLAlchemy."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        config = Config()
        self.db_path = Path(db_path or config.database_path())
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            connect_args={"check_same_thread": False, "timeout": 5},
        )
        event.listen(self.engine, "connect", self._on_connect)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self._lessons_cache: dict[tuple[str, str, str], tuple[float, list[dict[str, str]]]] = {}
        self._lessons_cache_lock = threading.RLock()

    @staticmethod
    def _on_connect(dbapi_connection, connection_record) -> None:
        """Включает WAL и ждёт до 5 c при блокировке БД.

        WAL позволяет парсеру (воркер-поток) писать расписание, пока бот
        параллельно читает его из основного потока — без взаимных блокировок.
        """
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    def init_db(self) -> None:
        """Создаёт таблицы и накатывает лёгкие миграции, если их ещё нет."""
        Base.metadata.create_all(self.engine)

        with self.engine.begin() as conn:
            columns = {col["name"] for col in inspect(conn).get_columns("file_versions")}
            if "parsed_version" not in columns:
                conn.execute(
                    text(
                        "ALTER TABLE file_versions "
                        "ADD COLUMN parsed_version INTEGER NOT NULL DEFAULT 0"
                    )
                )
                logger.info("Миграция: добавлена колонка file_versions.parsed_version")
                conn.execute(
                    text(
                        "UPDATE file_versions SET parsed_version = version "
                        "WHERE id IN (SELECT DISTINCT file_version_id FROM schedule_lessons)"
                    )
                )
                logger.info(
                    "Миграция: parsed_version выставлен для уже распарсенных версий"
                )

        user_columns = {
            col["name"] for col in inspect(self.engine).get_columns("bot_users")
        }
        if "role" not in user_columns:
            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE bot_users "
                        f"ADD COLUMN role VARCHAR(20) NOT NULL DEFAULT '{UserRole.STUDENT}'"
                    )
                )
                logger.info("Миграция: добавлена колонка bot_users.role")

        lesson_columns = {
            col["name"] for col in inspect(self.engine).get_columns("schedule_lessons")
        }
        if "course" not in lesson_columns:
            with self.engine.begin() as conn:
                conn.execute(
                    text("ALTER TABLE schedule_lessons ADD COLUMN course INTEGER")
                )
                logger.info("Миграция: добавлена колонка schedule_lessons.course")

        with self.engine.begin() as conn:
            updated = 0
            for name, file_version_id in conn.execute(
                text("SELECT name, id FROM file_versions")
            ):
                course = parse_course_from_filename(name)
                if course is None:
                    continue
                result = conn.execute(
                    text(
                        "UPDATE schedule_lessons SET course = :course "
                        "WHERE file_version_id = :file_version_id AND course IS NULL"
                    ),
                    {"course": course, "file_version_id": file_version_id},
                )
                updated += result.rowcount
            if updated:
                logger.info(
                    "Backfill: колонке schedule_lessons.course присвоен курс %d записям",
                    updated,
                )

        logger.info("База данных готова: %s", self.db_path)

    def mark_parsed(self, file_version_id: int, version: int) -> None:
        """Фиксирует, какая версия файла успешно распарсена."""
        with self.engine.begin() as conn:
            conn.execute(
                text("UPDATE file_versions SET parsed_version = :v WHERE id = :id"),
                {"v": version, "id": file_version_id},
            )
        logger.info("Версия #%d отмечена как распарсенная (v%d)", file_version_id, version)

    def schedule_table_empty(self) -> bool:
        """True, если в таблице расписания нет ни одной записи."""
        with Session(self.engine) as session:
            return session.scalar(select(func.count(ScheduleLesson.id))) == 0

    def register_file(
        self,
        name: str,
        size: int = 0,
        file_hash: str = "",
        source_url: str | None = None,
    ) -> tuple[int, bool]:
        """Регистрирует файл и возвращает (version, changed).

        changed=True — файла не было или содержимое изменилось
        (тогда версия увеличивается на 1).
        """
        with Session(self.engine) as session:
            record = session.scalar(select(FileVersion).where(FileVersion.name == name))
            if record is None:
                record = FileVersion(
                    name=name, version=1, size=size,
                    file_hash=file_hash, source_url=source_url,
                )
                session.add(record)
                session.commit()
                logger.info("Новый файл %r (v1, %d байт)", name, size)
                return 1, True

            if record.file_hash != file_hash:
                record.version += 1
                record.size = size
                record.file_hash = file_hash
                record.source_url = source_url
                logger.info("Файл %r обновлён: v%d (%d байт)", name, record.version, size)
                session.commit()
                return record.version, True

            session.commit()
            return record.version, False

    def get_file(self, name: str) -> FileVersion | None:
        with Session(self.engine) as session:
            return session.scalar(select(FileVersion).where(FileVersion.name == name))

    def hash_exists(self, file_hash: str) -> bool:
        """True, если файл с таким хэшем уже есть в БД."""
        with Session(self.engine) as session:
            return session.scalar(
                select(FileVersion.id).where(FileVersion.file_hash == file_hash)
            ) is not None

    def get_user(self, telegram_id: int) -> UserInfo | None:
        with Session(self.engine) as session:
            user = session.scalar(select(BotUser).where(BotUser.telegram_id == telegram_id))
            return UserInfo.from_model(user) if user else None

    def register_user(
        self,
        telegram_id: int,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        language_code: str | None = None,
        group_name: str | None = None,
        role: str = UserRole.STUDENT,
    ) -> UserInfo:
        """Регистрирует пользователя по telegram_id (upsert) и обновляет last_seen_at."""
        with Session(self.engine) as session:
            user = session.scalar(select(BotUser).where(BotUser.telegram_id == telegram_id))
            now = datetime.utcnow()
            if user is None:
                user = BotUser(
                    telegram_id=telegram_id, username=username,
                    first_name=first_name, last_name=last_name,
                    language_code=language_code, group_name=group_name,
                    role=role, is_active=True, last_seen_at=now,
                )
                session.add(user)
                session.commit()
                logger.info("Зарегистрирован новый пользователь %s (group=%r)", telegram_id, group_name)
            else:
                user.username = username
                user.first_name = first_name
                user.last_name = last_name
                user.language_code = language_code
                user.is_active = True
                user.last_seen_at = now
                if group_name is not None:
                    user.group_name = group_name
                session.commit()
                session.refresh(user)
            return UserInfo.from_model(user)

    def update_user_group(self, telegram_id: int, group_name: str) -> UserInfo | None:
        with Session(self.engine) as session:
            user = session.scalar(select(BotUser).where(BotUser.telegram_id == telegram_id))
            if user is None:
                return None
            user.group_name = group_name
            session.commit()
            session.refresh(user)
            logger.info("Пользователь %s выбрал группу %r", telegram_id, group_name)
            return UserInfo.from_model(user)

    def set_role(self, telegram_id: int, role: str) -> UserInfo | None:
        with Session(self.engine) as session:
            user = session.scalar(select(BotUser).where(BotUser.telegram_id == telegram_id))
            if user is None:
                return None
            user.role = role
            session.commit()
            session.refresh(user)
            logger.info("Пользователь %s получил роль %r", telegram_id, role)
            return UserInfo.from_model(user)

    def touch_user(self, telegram_id: int) -> None:
        """Обновляет время последней активности и статус is_active."""
        with Session(self.engine) as session:
            user = session.scalar(select(BotUser).where(BotUser.telegram_id == telegram_id))
            if user is None:
                return
            user.last_seen_at = datetime.utcnow()
            user.is_active = True
            session.commit()

    def log_action(self, telegram_id: int, action: str, payload: str | None = None) -> None:
        user_id = self._user_id(telegram_id)
        if user_id is None:
            return
        with Session(self.engine) as session:
            session.add(UserAction(user_id=user_id, action=action, payload=payload))
            session.commit()

    def _user_id(self, telegram_id: int) -> int | None:
        with Session(self.engine) as session:
            return session.scalar(
                select(BotUser.id).where(BotUser.telegram_id == telegram_id)
            )

    def list_groups(self) -> list[str]:
        """Уникальные группы из расписания (для кнопок выбора)."""
        with Session(self.engine) as session:
            rows = session.execute(
                select(ScheduleLesson.group_name)
                .where(ScheduleLesson.group_name != "")
                .distinct()
                .order_by(ScheduleLesson.group_name)
            ).scalars().all()
            return list(rows)

    def list_courses(self) -> list[int]:
        """Номера курсов, для которых есть расписание."""
        with Session(self.engine) as session:
            rows = session.execute(
                select(ScheduleLesson.course)
                .where(ScheduleLesson.course.is_not(None))
                .distinct()
                .order_by(ScheduleLesson.course)
            ).scalars().all()
            return list(rows)

    def list_groups_by_course(self, course: int) -> list[str]:
        """Уникальные группы указанного курса."""
        with Session(self.engine) as session:
            rows = session.execute(
                select(ScheduleLesson.group_name)
                .where(
                    ScheduleLesson.course == course,
                    ScheduleLesson.group_name != "",
                )
                .distinct()
                .order_by(ScheduleLesson.group_name)
            ).scalars().all()
            return list(rows)

    def get_course_by_group(self, group_name: str) -> int | None:
        """Курс группы по актуальному расписанию (None, если группа не найдена)."""
        with Session(self.engine) as session:
            return session.scalar(
                select(ScheduleLesson.course)
                .where(ScheduleLesson.group_name == group_name)
                .limit(1)
            )

    def list_users_by_course(self, course: int) -> list[UserInfo]:
        """Активные зарегистрированные пользователи, чьи группы относятся к курсу."""
        groups = set(self.list_groups_by_course(course))
        if not groups:
            return []
        with Session(self.engine) as session:
            users = session.scalars(
                select(BotUser).where(
                    BotUser.is_active.is_(True),
                    BotUser.group_name.is_not(None),
                )
            ).all()
            return [
                UserInfo.from_model(u)
                for u in users
                if u.group_name in groups
            ]

    def list_users_for_broadcast(self, target: int | str | None = None) -> list[UserInfo]:
        """Пользователи для рассылки: все / курс (int) / конкретная группа (str)."""
        if isinstance(target, int):
            return self.list_users_by_course(target)
        with Session(self.engine) as session:
            if isinstance(target, str):
                users = session.scalars(
                    select(BotUser).where(
                        BotUser.is_active.is_(True),
                        BotUser.group_name == target,
                    )
                ).all()
            else:
                users = session.scalars(
                    select(BotUser).where(
                        BotUser.is_active.is_(True),
                        BotUser.group_name.is_not(None),
                    )
                ).all()
            return [UserInfo.from_model(u) for u in users]

    def count_users_by_course(self, course: int) -> int:
        return len(self.list_users_by_course(course))

    def users_by_course_counts(self) -> dict[int, int]:
        return {
            course: len(self.list_users_by_course(course))
            for course in self.list_courses()
        }

    def search_users(self, query: str, limit: int = 30) -> list[UserInfo]:
        """Поиск пользователей по id/username/имени/группе (для управления ролями)."""
        q = f"%{query.strip().lower()}%"
        with Session(self.engine) as session:
            rows = session.execute(
                select(BotUser)
                .where(
                    or_(
                        func.cast(BotUser.telegram_id, String).like(q),
                        func.lower(BotUser.username).like(q),
                        func.lower(BotUser.first_name).like(q),
                        func.lower(BotUser.last_name).like(q),
                        func.lower(BotUser.group_name).like(q),
                    )
                )
                .order_by(BotUser.username)
                .limit(limit)
            ).scalars().all()
            return [UserInfo.from_model(u) for u in rows]

    def list_groups_for_file(self, file_version_id: int) -> list[str]:
        """Уникальные группы, затронутые версией файла."""
        with Session(self.engine) as session:
            rows = session.execute(
                select(ScheduleLesson.group_name)
                .where(
                    ScheduleLesson.file_version_id == file_version_id,
                    ScheduleLesson.group_name != "",
                )
                .distinct()
                .order_by(ScheduleLesson.group_name)
            ).scalars().all()
            return list(rows)

    def get_file_for_course(self, course: int) -> FileVersion | None:
        """Актуальный (с максимальной версией) файл расписания курса."""
        candidate: FileVersion | None = None
        for record in self.list_files():
            if parse_course_from_filename(record.name) != course:
                continue
            if candidate is None or record.version > candidate.version:
                candidate = record
        return candidate

    def get_lessons_for_group(
        self, group_name: str, week: str, day_of_week: str
    ) -> list[dict[str, str]]:
        """Занятия группы на день и неделю, отсортированные по времени.

        Результат кэшируется на CACHE_TTL_SECONDS (ключ — группа+неделя+день).
        Строки с пустой дисциплиной (артефакты PDF) пропускаются.
        """
        ttl = Config().cache_ttl_seconds()
        key = (group_name, week, day_of_week)
        now = time.time()
        with self._lessons_cache_lock:
            hit = self._lessons_cache.get(key)
            if hit is not None and hit[0] > now:
                return [dict(row) for row in hit[1]]
        lessons = self._load_lessons_for_group(group_name, week, day_of_week)
        with self._lessons_cache_lock:
            self._lessons_cache[key] = (now + ttl, lessons)
            return [dict(row) for row in lessons]

    def _load_lessons_for_group(
        self, group_name: str, week: str, day_of_week: str
    ) -> list[dict[str, str]]:
        with Session(self.engine) as session:
            rows = session.execute(
                select(
                    ScheduleLesson.time,
                    ScheduleLesson.discipline,
                    ScheduleLesson.lesson_type,
                    ScheduleLesson.audience,
                    ScheduleLesson.teacher,
                )
                .where(
                    ScheduleLesson.group_name == group_name,
                    ScheduleLesson.week == week,
                    ScheduleLesson.day_of_week == day_of_week,
                    ScheduleLesson.discipline != "",
                )
                .order_by(ScheduleLesson.time)
            ).all()
            return [
                {
                    "time": time,
                    "discipline": discipline,
                    "lesson_type": lesson_type,
                    "audience": audience,
                    "teacher": teacher,
                }
                for time, discipline, lesson_type, audience, teacher in rows
            ]

    def invalidate_lessons_cache(self) -> None:
        """Сбрасывает кэш расписания (вызывается после обновления БД)."""
        with self._lessons_cache_lock:
            self._lessons_cache.clear()
            count = len(self._lessons_cache)
        logger.debug("Кэш расписания сброшен (%d записей)", count)

    def count_total_users(self) -> int:
        with Session(self.engine) as session:
            return session.scalar(select(func.count(BotUser.id))) or 0

    def count_active_users(self, days: int = 30) -> int:
        with Session(self.engine) as session:
            threshold = datetime.utcnow() - timedelta(days=days)
            return session.scalar(
                select(func.count(BotUser.id)).where(BotUser.last_seen_at >= threshold)
            ) or 0

    def action_stats(self, days: int = 30) -> dict[str, int]:
        """Количество действий по типам за период."""
        with Session(self.engine) as session:
            threshold = datetime.utcnow() - timedelta(days=days)
            rows = session.execute(
                select(UserAction.action, func.count(UserAction.id))
                .where(UserAction.created_at >= threshold)
                .group_by(UserAction.action)
            ).all()
            return {action: count for action, count in rows}

    def list_files(self) -> list[FileVersion]:
        with Session(self.engine) as session:
            return list(session.scalars(select(FileVersion).order_by(FileVersion.name)))

    def replace_lessons_for_version(
        self, file_version_id: int, lessons: list[ScheduleLesson]
    ) -> int:
        """Заменяет все занятия версии файла новой пачкой в одной транзакции.

        Удаляет старые записи этой версии и вставляет lessons целиком,
        без записи по одному. Возвращает количество вставленных записей.
        """
        if file_version_id is None:
            raise ValueError("file_version_id обязателен")
        with Session(self.engine) as session:
            session.execute(
                delete(ScheduleLesson).where(
                    ScheduleLesson.file_version_id == file_version_id
                )
            )
            session.add_all(lessons)
            session.commit()
            logger.info(
                "Версия #%d: заменено записей расписания: %d", file_version_id, len(lessons)
            )
            self.invalidate_lessons_cache()
            return len(lessons)

    def ban_report_user(self, telegram_id: int) -> None:
        """Запрещает пользователю отправлять сообщения о проблеме/предложении."""
        with Session(self.engine) as session:
            existing = session.get(ReportBan, telegram_id)
            if existing is None:
                session.add(ReportBan(telegram_id=telegram_id))
                session.commit()
                logger.info("Пользователь %s забанен на сообщения о проблеме", telegram_id)

    def is_report_banned(self, telegram_id: int) -> bool:
        """True, если пользователь не может отправлять сообщения о проблеме."""
        with Session(self.engine) as session:
            return session.get(ReportBan, telegram_id) is not None


def file_sha256(path: str | Path) -> str:
    """Считает SHA-256 хэш файла кусками."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    """Считает SHA-256 хэш содержимого из памяти."""
    return hashlib.sha256(data).hexdigest()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    db = Database()
    db.init_db()
    print("Файлы в базе:")
    for f in db.list_files():
        print(f"  - {f.name} (v{f.version}, {f.size} байт, обновлён {f.updated_at})")