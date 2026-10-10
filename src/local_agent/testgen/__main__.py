"""Долгий прогон генерации по всей документации из командной строки; прерванный запуск продолжается с места остановки.

    uv run python -m local_agent.testgen softwlc_md/v1.37_SoftWLC --workspace ~/my_projects/job_wifi --limit 5
"""

import argparse
import asyncio
import sys
from pathlib import Path

from local_agent.config.settings import get_settings
from local_agent.llm.openai_compatible import OpenAICompatibleProvider
from local_agent.testgen.pipeline import CHUNK_CHARS, MAX_CASES, CaseGenerator, PageResult, summary


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(prog="python -m local_agent.testgen", description=__doc__.splitlines()[0])
    parser.add_argument("section", help="папка или страница документации относительно рабочей папки")
    parser.add_argument("--workspace", default=".", help="рабочая папка проекта (по умолчанию текущая)")
    parser.add_argument("--model", default=settings.default_model, help="модель (по умолчанию DEFAULT_MODEL)")
    parser.add_argument("--url", default=settings.llm_base_url, help="OpenAI-compatible сервер (по умолчанию LLM_BASE_URL)")
    parser.add_argument("--timeout", type=float, default=600, help="тайм-аут одного запроса к модели, с")
    parser.add_argument("--limit", type=int, default=0, help="обработать не больше N страниц (0 — все)")
    parser.add_argument("--force", action="store_true", help="перегенерировать уже готовые страницы")
    parser.add_argument("--chunk-chars", type=int, default=CHUNK_CHARS, help="размер куска страницы в символах")
    parser.add_argument("--max-cases", type=int, default=MAX_CASES, help="не больше N кейсов на кусок")
    args = parser.parse_args()
    if not args.model:
        parser.error("укажите --model или DEFAULT_MODEL в .env")
    root = Path(args.workspace).expanduser().resolve()
    start = (root / args.section).resolve()
    if not start.is_relative_to(root) or not start.exists():
        parser.error(f"раздел {args.section} не найден в рабочей папке {root}")
    sys.exit(asyncio.run(run(args, root, start)))


async def run(args: argparse.Namespace, root: Path, start: Path) -> int:
    provider = OpenAICompatibleProvider(args.url, args.timeout, name="LLM")
    generator = CaseGenerator(provider, args.model, root, chunk_chars=args.chunk_chars, max_cases=args.max_cases)

    def progress(page: PageResult) -> None:
        if page.output:
            print(f"+ {page.output}: кейсов {page.cases}, замечаний по стилю {page.style_issues}", flush=True)
        elif page.errors:
            print(f"! {page.relative}: {'; '.join(page.errors)}", flush=True)

    try:
        batch = await generator.run(start, limit=args.limit, force=args.force, on_page=progress)
    finally:
        await provider.close()
    print("\n" + summary(batch))
    return 1 if any(page.errors for page in batch.pages) else 0


if __name__ == "__main__":
    main()
