"""
STALCRAFT auction history collector - для запуска по расписанию в
GitHub Actions, НЕЗАВИСИМО от того, включён ли твой личный компьютер.

Использует тот же storage.py, что и основное приложение (app.py) - та
же логика похода в API, тот же формат SQLite-базы. Просто открывает
stalcraft_history.db (создаёт, если нет), дописывает в неё новые
продажи и выходит - изменённый файл базы дальше коммитит сам workflow
(.github/workflows/collector.yml).

Требуемые переменные окружения (в GitHub Actions - через Secrets):
- STALCRAFT_CLIENT_ID
- STALCRAFT_CLIENT_SECRET
- STALCRAFT_API_MODE (production или demo, по умолчанию production)
- STALCRAFT_REGION (по умолчанию EU)
"""

import sys
import time

import requests
import storage


def main():
    storage.init_db()

    total_inserted = 0
    errors = 0

    for i, item in enumerate(storage.ARTIFACTS):
        item_id = item["id"]
        try:
            resp = storage.get_price_history(item_id)
        except requests.RequestException as e:
            errors += 1
            print(f"[collector] ошибка на {item_id} ({item['name_en']}): {e}")
            if storage.REQUEST_DELAY:
                time.sleep(storage.REQUEST_DELAY)
            continue

        entries = resp.get("prices", resp) if isinstance(resp, dict) else resp
        inserted = storage.store_sales(item_id, entries or [])
        total_inserted += inserted

        if storage.REQUEST_DELAY and i < len(storage.ARTIFACTS) - 1:
            time.sleep(storage.REQUEST_DELAY)

    print(
        f"[collector] новых записей: {total_inserted}, "
        f"всего в базе: {storage.db_total_rows()}, "
        f"предметов с ошибками: {errors}"
    )

    if errors == len(storage.ARTIFACTS):
        # Все запросы провалились (скорее всего протух токен или сеть) -
        # выходим с ошибкой, чтобы GitHub Actions явно показал "failed",
        # а не тихо закоммитил пустой результат как будто всё ок.
        sys.exit(1)


if __name__ == "__main__":
    main()
