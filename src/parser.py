import logging
import re
import sys
import time
from pathlib import Path

import pdfplumber
from pdfplumber.page import Page
from pdfplumber.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import Config  # noqa: E402
from src.database import Database, ScheduleLesson, parse_course_from_filename  # noqa: E402

logger = logging.getLogger("parser")

DAY_NAMES = {
    "ПОНЕДЕЛЬНИК": "ПОНЕДЕЛЬНИК",
    "ВТОРНИК": "ВТОРНИК",
    "СРЕДА": "СРЕДА",
    "ЧЕТВЕРГ": "ЧЕТВЕРГ",
    "ПЯТНИЦА": "ПЯТНИЦА",
    "СУББОТА": "СУББОТА",
    "ВОСКРЕСЕНЬЕ": "ВОСКРЕСЕНЬЕ",
}
DAY_ORDER = list(DAY_NAMES)

GROUP_RE = re.compile(r"Группа\s+(\S(?:.*?)?)(?=\n|$)")


def find_schedule_files(dest_dir: Path | str) -> list[Path]:
    """Возвращает PDF-файлы с расписанием, в имени которых есть 'KURS'."""
    dest_dir = Path(dest_dir)
    files = sorted(p for p in dest_dir.glob("*.pdf") if "KURS" in p.name.upper())
    logger.info("Найдено файлов расписания: %d", len(files))
    return files


def _day_labels(page: Page) -> list[tuple[str, float]]:
    """Достаёт вертикальные подписи дней недели (повёрнутый текст).

    Возвращает список (НАЗВАНИЕ_ДНЯ, top, bottom), отсортированный сверху вниз.
    """
    labels: list[tuple[str, float, float]] = []
    for word in page.extract_words():
        text = word["text"].strip()
        if not text:
            continue
        reversed_text = text[::-1].upper().strip("._- ")
        if reversed_text in DAY_NAMES:
            labels.append((DAY_NAMES[reversed_text], word["top"], word["bottom"]))
    labels.sort(key=lambda item: (item[1], item[2]))
    return labels


def _day_for_row(row_top: float, labels: list[tuple[str, float]]) -> str:
    """Определяет день недели строки по вертикальным меткам.

    Границей между соседними днями считается середина между центрами меток.
    """
    if not labels:
        return ""
    centers = [(day, (top + bottom) / 2) for day, top, bottom in labels]
    day = centers[0][0]
    for i in range(len(centers) - 1):
        boundary = (centers[i][1] + centers[i + 1][1]) / 2
        if row_top > boundary:
            day = centers[i + 1][0]
    return day


def _fill_missing_leading_days(
    labels: list[tuple[str, float, float]], table_top: float
) -> list[tuple[str, float, float]]:
    """Дозаполняет отсутствующие ведущие метки дней.

    Изредка «ПОНЕДЕЛЬНИК» (или несколько дней подряд) нарисован на странице
    векторными кривыми, а не текстом — pdfplumber его не извлекает, и первая
    найденная метка оказывается не ПОНЕДЕЛЬНИК. Тогда все строки выше неё
    ошибочно приписываются этой метке, и расписание «теряет» понедельник.

    Полосы дней идут равными по высоте с равным шагом top-координат меток,
    поэтому недостающие ведущие метки синтезируются наверх тем же шагом.
    """
    if not labels or labels[0][0] == DAY_NAMES["ПОНЕДЕЛЬНИК"]:
        return labels
    spacing = labels[1][1] - labels[0][1] if len(labels) > 1 else 115.0
    first_idx = DAY_ORDER.index(labels[0][0])
    height = labels[0][2] - labels[0][1]
    result = list(labels)
    for k in range(1, first_idx + 1):
        new_top = labels[0][1] - spacing * k
        if new_top < table_top - spacing * 0.5:
            break
        result.insert(0, (DAY_ORDER[first_idx - k], new_top, new_top + height))
    return result


def _group_name(page: Page) -> str:
    text = page.extract_text() or ""
    match = GROUP_RE.search(text)
    if not match:
        raise ValueError("Не найдена строка 'Группа ...' на странице")
    return match.group(1).strip()


def _cell_text(cell) -> str:
    """Нормализует ячейку таблицы (строка или кортеж при объединении)."""
    if isinstance(cell, tuple):
        cell = cell[0] if cell else None
    if cell is None:
        return ""
    return str(cell).strip()


def _column_map(header_row: list) -> dict[str, int]:
    """Определяет, какой индекс таблицы соответствует какой колонке."""
    normalized = {
        "время": "time",
        "неделя": "week",
        "дисциплина": "discipline",
        "ауд": "audience",
        "вид занятия": "lesson_type",
        "преподаватель": "teacher",
    }
    mapping: dict[str, int] = {}
    for idx, cell in enumerate(header_row):
        text = _cell_text(cell).lower()
        for key, field in normalized.items():
            if key in text:
                mapping[field] = idx
    return mapping


def _extract_page_lessons(
    page: Page, file_version_id: int, group_name: str, course: int | None
) -> list[ScheduleLesson]:
    tables = page.find_tables()
    if not tables:
        logger.warning("На странице нет таблицы: %s", group_name)
        return []

    table: Table = tables[0]
    text_rows = table.extract()
    labels = _day_labels(page)
    if labels:
        table_top = min(row.bbox[1] for row in table.rows)
        labels = _fill_missing_leading_days(labels, table_top)
    mapping: dict[str, int] = {}

    lessons: list[ScheduleLesson] = []
    for row, cells in zip(table.rows, text_rows):
        if not mapping:
            if any(_cell_text(c).lower() == "время" for c in cells):
                mapping = _column_map(cells)
                continue
            continue

        def cell(field: str) -> str:
            idx = mapping.get(field)
            if idx is None or idx >= len(cells):
                return ""
            return _cell_text(cells[idx])

        time = cell("time")
        if not time:
            continue

        lessons.append(
            ScheduleLesson(
                file_version_id=file_version_id,
                course=course,
                group_name=group_name,
                day_of_week=_day_for_row(row.bbox[1], labels),
                time=time,
                week=cell("week"),
                discipline=cell("discipline"),
                audience=cell("audience"),
                lesson_type=cell("lesson_type"),
                teacher=cell("teacher"),
            )
        )
    return lessons


def extract_lessons(pdf_path: Path, file_version_id: int) -> list[ScheduleLesson]:
    """Извлекает все занятия из PDF-файла (каждая страница = группа)."""
    course = parse_course_from_filename(pdf_path.name)
    lessons: list[ScheduleLesson] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            try:
                group = _group_name(page)
                page_lessons = _extract_page_lessons(
                    page, file_version_id, group, course
                )
                lessons.extend(page_lessons)
                logger.info(
                    "Стр. %d: %s — %d занятий", page_no, group, len(page_lessons)
                )
            except Exception as exc:
                logger.warning("Стр. %d: не удалось распарсить (%s)", page_no, exc)
    logger.info("Файл %s: всего занятий — %d", pdf_path.name, len(lessons))
    return lessons


def parse_schedule_files(
    dest_dir: Path | str | None = None, db: Database | None = None
) -> dict[str, dict[str, float | int | str]]:
    """Парсит только изменённые файлы расписания и пакетно записывает их в БД.

    Файл парсится, если таблица занятий пуста (полный первичный парсинг) или
    если версия файла не была распарсена раньше (parsed_version != version).
    Для каждого файла: delete + bulk insert за одну транзакцию.
    Возвращает сводку {file: {count, seconds, status}}.
    """
    db = db or Database()
    config = Config()
    dest_dir = Path(dest_dir or config.download_dir())
    files = find_schedule_files(dest_dir)

    full_parse = db.schedule_table_empty()
    if full_parse:
        logger.info("Таблица расписания пуста — парсинг всех файлов")

    report: dict[str, dict[str, float | int | str]] = {}
    for pdf_path in files:
        t_start = time.perf_counter()
        file_record = db.get_file(pdf_path.name)
        if file_record is None:
            logger.warning(
                "Файл %s не найден в БД (не скачивался downloader'ом) — пропуск",
                pdf_path.name,
            )
            continue

        up_to_date = file_record.parsed_version == file_record.version
        if not full_parse and up_to_date:
            logger.info(
                "Решение: %s — актуально (v%d уже распарсена), пропуск",
                pdf_path.name, file_record.version,
            )
            report[pdf_path.name] = {
                "count": 0,
                "seconds": round(time.perf_counter() - t_start, 2),
                "status": "skipped",
            }
            continue

        reason = "таблица пуста" if full_parse else f"версия изменилась v{file_record.parsed_version}→v{file_record.version}"
        logger.info(
            "Решение: %s — парсим (%s)", pdf_path.name, reason,
        )

        lessons = extract_lessons(pdf_path, file_record.id)
        count = db.replace_lessons_for_version(file_record.id, lessons)
        seconds = round(time.perf_counter() - t_start, 2)
        if count:
            db.mark_parsed(file_record.id, file_record.version)
            report[pdf_path.name] = {
                "count": count,
                "seconds": seconds,
                "status": "parsed",
            }
            logger.info("Файл %s распарсен за %.2f c", pdf_path.name, seconds)
        else:
            report[pdf_path.name] = {
                "count": 0,
                "seconds": seconds,
                "status": "empty",
            }
            logger.warning(
                "Файл %s не дал ни одного занятия — версия НЕ отмечена как распарсенная",
                pdf_path.name,
            )
    return report


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    import argparse

    parser = argparse.ArgumentParser(description="Парсинг расписания из PDF в БД")
    parser.add_argument("--dir", default=None, help="Каталог с PDF-файлами расписания")
    args = parser.parse_args()

    config = Config()
    dest_dir = Path(args.dir) if args.dir else Path(config.download_dir())

    db = Database()
    db.init_db()

    t_total = time.perf_counter()
    report = parse_schedule_files(dest_dir, db=db)
    total_seconds = time.perf_counter() - t_total

    parsed = [item for item in report.values() if item["status"] == "parsed"]
    skipped = [item for item in report.values() if item["status"] == "skipped"]
    empty = [item for item in report.values() if item["status"] == "empty"]
    total = sum(item["count"] for item in report.values())
    print("\n" + "=" * 56)
    print("ПАРСИНГ РАСПИСАНИЯ ЗАВЕРШЁН")
    print("=" * 56)
    for file_name, item in report.items():
        if item["status"] == "parsed":
            print(f"  [{item['seconds']:>7.2f} c] записано {item['count']:>5} | {file_name}")
        elif item["status"] == "skipped":
            print(f"  [      -  c] актуально        | {file_name}")
        else:
            print(f"  [{item['seconds']:>7.2f} c] 0 занятий!       | {file_name}")
    print("-" * 56)
    print(f"  Распарсено: {len(parsed)}, пропущено (актуально): {len(skipped)}, пустых: {len(empty)}")
    print(f"  Записано в этом запуске: {total}")
    print(f"  Время парсинга: {sum(item['seconds'] for item in parsed):.2f} c"
          f" (всего запуск: {total_seconds:.2f} c)")
    print("=" * 56)


if __name__ == "__main__":
    main()