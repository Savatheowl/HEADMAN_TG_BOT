import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("config")

BASE_DIR = Path(__file__).resolve().parent.parent


def _parse_env_file(path: Path) -> dict[str, str]:
    """Парсит .env файл в словарь. Кидает исключения при проблемах с файлом."""
    if not path.exists():
        raise FileNotFoundError(f".env файл не найден: {path}")

    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OSError(f"Не удалось прочитать .env файл {path}: {exc}") from exc

    values: dict[str, str] = {}
    for line_no, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            logger.warning("[config] Пропущена строка %d (нет '='): %r", line_no, line)
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        values[key] = value
    return values


class Config:
    """Доступ к значениям из .env файла и переменных окружения.

    Приоритет: переменные окружения ОС > значения из .env файла.
    Для необязательных переменных задаются fallback-значения по умолчанию.
    """

    DEFAULTS: dict[str, Any] = {
        "DATABASE_PATH": str(BASE_DIR / "data" / "bot.db"),
        "LOG_LEVEL": "INFO",
        "UPDATE_INTERVAL_MINUTES": 30,
        "DOWNLOAD_DIR": str(BASE_DIR / "downloads"),
        "CACHE_TTL_SECONDS": 3600,
    }

    def __init__(self, env_path: Path | str | None = None) -> None:
        self._env_path = Path(env_path or BASE_DIR / ".env")
        self._values: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        try:
            self._values = _parse_env_file(self._env_path)
            logger.info("[config] Загружен .env файл: %s", self._env_path)
        except FileNotFoundError as exc:
            logger.warning("[config] %s. Используются переменные окружения и fallback-значения.", exc)
            self._values = {}
        except OSError as exc:
            logger.error("[config] %s. Используются переменные окружения и fallback-значения.", exc)
            self._values = {}

    def _get_raw(self, key: str) -> str | None:
        if key in os.environ:
            return os.environ[key]
        return self._values.get(key)

    def get(self, key: str, fallback: Any = None) -> Any:
        """Возвращает строковое значение переменной или fallback."""
        value = self._get_raw(key)
        if value is None:
            return fallback
        return value

    def get_int(self, key: str, fallback: Any = None) -> Any:
        value = self._get_raw(key)
        if value is None:
            return fallback
        try:
            return int(value)
        except ValueError as exc:
            logger.warning("[config] Переменная %s=%r не является числом: %s. Используется fallback %r.",
                           key, value, exc, fallback)
            return fallback

    def get_bool(self, key: str, fallback: Any = None) -> Any:
        value = self._get_raw(key)
        if value is None:
            return fallback
        return value.strip().lower() in ("1", "true", "yes", "on", "y")

    def get_json(self, key: str, fallback: Any = None) -> Any:
        import json

        value = self._get_raw(key)
        if value is None:
            return fallback
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("[config] Переменная %s=%r не является JSON: %s. Используется fallback %r.",
                           key, value, exc, fallback)
            return fallback

    def require(self, key: str) -> str:
        """Возвращает обязательное значение или кидает RuntimeError с логом."""
        value = self._get_raw(key)
        if value is None:
            logger.error(
                "[config] Обязательная переменная %r не задана ни в .env, ни в окружении.", key
            )
            raise RuntimeError(
                f"Обязательная переменная {key!r} отсутствует. "
                f"Проверьте {self._env_path} (пример: .env.example)."
            )
        return value

    def bot_token(self) -> str:
        return self.require("BOT_TOKEN")

    def database_path(self) -> str:
        return self.get("DATABASE_PATH", self.DEFAULTS["DATABASE_PATH"])

    def log_level(self) -> str:
        return self.get("LOG_LEVEL", self.DEFAULTS["LOG_LEVEL"])

    def update_interval_minutes(self) -> int:
        return self.get_int("UPDATE_INTERVAL_MINUTES", self.DEFAULTS["UPDATE_INTERVAL_MINUTES"])

    def download_dir(self) -> str:
        return self.get("DOWNLOAD_DIR", self.DEFAULTS["DOWNLOAD_DIR"])

    def cache_ttl_seconds(self) -> int:
        return self.get_int("CACHE_TTL_SECONDS", self.DEFAULTS["CACHE_TTL_SECONDS"])


if __name__ == "__main__":
    pass