import asyncio
import logging
import re
import sys
import time
from pathlib import Path

from playwright.async_api import BrowserContext, Page, async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import BASE_DIR, Config  # noqa: E402
from src.database import Database, sha256_bytes  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_TOPIC_URL = "https://vk.ru/topic-61699714_47589048"
VK_BASE = "https://vk.ru"

SELECTOR_DOC_TITLE = "a.page_doc_title"
SELECTOR_PDF_URL = 'input[name="url"]'
SELECTOR_PDF_NAME = 'input[name="ext"]'
SELECTOR_FILE_NAME = 'input[name="name"]'

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


def sanitize_filename(name: str, default: str = "schedule.pdf") -> str:
    """Оставляет в имени файла только безопасные символы."""
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("._")
    return cleaned or default


async def fetch_doc_links(page: Page, topic_url: str) -> list[dict[str, str]]:
    """Открывает страницу темы и собирает ссылки на документы."""
    logger.info("Открываю страницу темы: %s", topic_url)
    await page.goto(topic_url, wait_until="domcontentloaded", timeout=30_000)
    await page.wait_for_selector(SELECTOR_DOC_TITLE, timeout=30_000)

    raw = await page.eval_on_selector_all(
        SELECTOR_DOC_TITLE,
        "els => els.map(e => ({href: e.getAttribute('href'), text: e.textContent.trim()}))",
    )

    links: list[dict[str, str]] = []
    for item in raw:
        href = (item.get("href") or "").strip()
        if not href:
            continue
        if not href.startswith("http"):
            href = VK_BASE + href
        text = (item.get("text") or "").strip()
        logger.info("Найдена ссылка на документ: %s (%s)", href[:80], text or "без названия")
        links.append({"url": href, "title": text})

    logger.info("Всего ссылок на документы: %d", len(links))
    return links


async def fetch_pdf_url(page: Page, doc_url: str) -> dict[str, str]:
    """Открывает страницу документа и достаёт прямую ссылку на PDF."""
    await page.goto(doc_url, wait_until="domcontentloaded", timeout=30_000)
    url_input = page.locator(SELECTOR_PDF_URL)
    await url_input.wait_for(state="attached", timeout=15_000)

    pdf_url = await url_input.get_attribute("value")
    file_name = await page.locator(SELECTOR_FILE_NAME).get_attribute("value")
    ext = await page.locator(SELECTOR_PDF_NAME).get_attribute("value")

    if not pdf_url:
        raise RuntimeError(f"На странице {doc_url} не удалось получить value у input[name='url']")

    return {
        "pdf_url": pdf_url,
        "file_name": sanitize_filename(file_name or "", f"schedule.{ext or 'pdf'}"),
    }


async def download_pdf(context: BrowserContext, pdf_url: str) -> bytes:
    """Скачивает PDF по прямой ссылке в память и возвращает его байты."""
    response = await context.request.get(pdf_url, timeout=60_000)
    if not response.ok:
        raise RuntimeError(
            f"Ошибка скачивания {pdf_url[:80]}: HTTP {response.status}"
        )
    return await response.body()


async def run_downloader(
    topic_url: str = DEFAULT_TOPIC_URL,
    dest_dir: Path | str = BASE_DIR / "downloads",
    concurrency: int = 3,
) -> dict[str, object]:
    """Качает все расписания из темы. Возвращает сводку с замерами времени."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    db = Database()

    start_total = time.perf_counter()
    results: list[dict[str, object]] = []

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=USER_AGENT)
        page = await context.new_page()

        try:
            doc_links = await fetch_doc_links(page, topic_url)
        finally:
            await page.close()

        semaphore = asyncio.Semaphore(concurrency)

        async def process(doc: dict[str, str]) -> None:
            async with semaphore:
                doc_page = await context.new_page()
                t_start = time.perf_counter()
                try:
                    fetched = await fetch_pdf_url(doc_page, doc["url"])
                    pdf_url = fetched["pdf_url"]
                    file_name = fetched["file_name"]

                    t_dl = time.perf_counter()
                    body = await download_pdf(context, pdf_url)
                    size = len(body)
                    file_hash = sha256_bytes(body)
                    elapsed = time.perf_counter() - t_start
                    dl_elapsed = time.perf_counter() - t_dl

                    if db.hash_exists(file_hash):
                        logger.info(
                            "Решение: %s — НЕ обновлять (хэш %s уже есть в БД)",
                            file_name, file_hash[:12],
                        )
                        results.append(
                            {
                                "file": file_name,
                                "size": size,
                                "seconds": round(elapsed, 2),
                                "download_seconds": round(dl_elapsed, 2),
                                "status": "skipped",
                            }
                        )
                        return

                    dest = dest_dir / file_name
                    dest.write_bytes(body)
                    version, changed = db.register_file(
                        name=file_name,
                        size=size,
                        file_hash=file_hash,
                        source_url=doc["url"],
                    )
                    action = "НОВЫЙ файл" if version == 1 else f"обновление до v{version}"
                    logger.info(
                        "Решение: %s — %s (v%d, %d байт) за %.2f c",
                        file_name, action, version, size, elapsed,
                    )
                    results.append(
                        {
                            "file": file_name,
                            "size": size,
                            "version": version,
                            "seconds": round(elapsed, 2),
                            "download_seconds": round(dl_elapsed, 2),
                            "status": "saved",
                        }
                    )
                except Exception as exc:
                    logger.error("Ошибка обработки %s: %s", doc["url"], exc)
                    results.append(
                        {
                            "file": doc["title"] or Path(doc["url"]).name,
                            "size": 0,
                            "seconds": round(time.perf_counter() - t_start, 2),
                            "error": str(exc),
                        }
                    )
                finally:
                    await doc_page.close()

        await asyncio.gather(*(process(doc) for doc in doc_links))

        await context.close()
        await browser.close()

    saved = [r for r in results if r.get("status") == "saved"]
    skipped = [r for r in results if r.get("status") == "skipped"]
    errors = [r for r in results if "error" in r]

    if saved:
        logger.info(
            "Решение по обновлению расписания: ОБНОВЛЕНО — сохранено %d файлов"
            " (пропущено %d, ошибок %d)",
            len(saved), len(skipped), len(errors),
        )
    elif errors:
        logger.warning(
            "Решение по обновлению расписания: НЕ ОПРЕДЕЛЕНО — %d ошибок, изменений нет",
            len(errors),
        )
    else:
        logger.info(
            "Решение по обновлению расписания: БЕЗ ИЗМЕНЕНИЙ — все %d файлов уже актуальны",
            len(skipped),
        )

    total = time.perf_counter() - start_total
    return {
        "results": results,
        "total": total,
    }


def _print_report(report: dict[str, object]) -> None:
    """Печатает сводку с замерами скорости."""
    results = report["results"]
    total = report["total"]
    saved = [r for r in results if r.get("status") == "saved"]
    skipped = [r for r in results if r.get("status") == "skipped"]
    errors = [r for r in results if "error" in r]

    print("\n" + "=" * 52)
    print("СВОДКА СКАЧИВАНИЯ РАСПИСАНИЙ")
    print("=" * 52)
    for r in results:
        if "error" in r:
            print(f"  [{r['seconds']:>6.2f} c] ОШИБКА      {r['file']}: {r['error']}")
        elif r.get("status") == "skipped":
            print(f"  [{r['seconds']:>6.2f} c] ПРОПУЩЕН    {r['file']} (хэш уже в БД, {r['size']:,} байт)")
        else:
            dl = r.get("download_seconds", r["seconds"])
            version = r.get("version", "-")
            print(f"  [{r['seconds']:>6.2f} c, {dl:.2f} c скач.] v{version}  {r['file']} ({r['size']:,} байт)")
    print("-" * 52)
    print(f"  Сохранено: {len(saved)}, пропущено: {len(skipped)}, ошибок: {len(errors)}")
    print(f"  Всего заняло:   {total:.2f} c")
    avg = (total / len(results)) if results else 0
    print(f"  В среднем на документ: {avg:.2f} c")
    print("=" * 52)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    import argparse

    parser = argparse.ArgumentParser(description="Скачивание расписаний из VK")
    parser.add_argument("--url", default=DEFAULT_TOPIC_URL, help="URL темы с расписаниями")
    parser.add_argument("--dir", default=None, help="Каталог для сохранения PDF")
    parser.add_argument("--concurrency", type=int, default=3, help="Параллельных страниц")
    args = parser.parse_args()

    config = Config()
    dest_dir = Path(args.dir) if args.dir else Path(config.download_dir())

    report = asyncio.run(run_downloader(args.url, dest_dir, args.concurrency))
    _print_report(report)


if __name__ == "__main__":
    main()