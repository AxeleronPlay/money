"""
Общий модуль: конфигурация, клиент STALCRAFT API и локальное хранилище
истории продаж (SQLite).

Используется и локальным веб-приложением (app.py), и облачным
сборщиком (collector.py, запускается по расписанию в GitHub Actions,
независимо от того, включён ли твой компьютер) - чтобы не дублировать
логику похода в API и записи в базу в двух местах.
"""

import os
import json
import time
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

# --------------------------------------------------------------------------
# Конфигурация
# --------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent

# grant type: "demo" or "production"
API_MODE = os.environ.get("STALCRAFT_API_MODE", "demo").lower()

REGION = os.environ.get("STALCRAFT_REGION", "EU")

# Demo credentials are public, taken straight from the official docs
# (https://eapi.stalcraft.net/auth.html). They only work against dapi.*
DEMO_CLIENT_ID = "1"
DEMO_CLIENT_SECRET = "E98cm6J9NNjTQopph0c2eIXNKafg4R1Cjz0TZh2D"

if API_MODE == "production":
    BASE_URL = "https://eapi.stalcraft.net"
    CLIENT_ID = os.environ["STALCRAFT_CLIENT_ID"]        # must be set
    CLIENT_SECRET = os.environ["STALCRAFT_CLIENT_SECRET"]  # must be set
else:
    BASE_URL = "https://dapi.stalcraft.net"
    CLIENT_ID = os.environ.get("STALCRAFT_CLIENT_ID", DEMO_CLIENT_ID)
    CLIENT_SECRET = os.environ.get("STALCRAFT_CLIENT_SECRET", DEMO_CLIENT_SECRET)

HEADERS = {
    "Client-Id": CLIENT_ID,
    "Client-Secret": CLIENT_SECRET,
}

# Файл локальной базы, куда копится вся когда-либо увиденная история
# продаж (свой архив, независимый от того, сколько глубины отдаёт сам
# STALCRAFT API). Растёт со временем работы инструмента - и локального,
# и облачного сборщика (они пишут в ОДИН И ТОТ ЖЕ файл/репозиторий).
STORAGE_DB_PATH = BASE_DIR / os.environ.get("STORAGE_DB_FILE", "stalcraft_history.db")

# Сколько записей истории тянуть за один запрос. ВАЖНО: похоже, что API
# не принимает limit больше 200 (при 300 отдаёт 400 Bad Request) - не
# ставь больше без проверки.
HISTORY_FETCH_LIMIT = min(int(os.environ.get("HISTORY_FETCH_LIMIT", "200")), 200)

# Пауза между запросами к API (сек) - защита от 429 Too Many Requests.
REQUEST_DELAY = float(os.environ.get("REQUEST_DELAY_SECONDS", "0.3"))

with open(BASE_DIR / "artifacts.json", encoding="utf-8") as f:
    ARTIFACTS = json.load(f)

# Индекс "qlt" (редкость) из additional-данных лота/продажи.
RARITY_NAMES = {
    0: "обычный",
    1: "необычный",
    2: "особый",
    3: "редкий",
    4: "исключительный",
    5: "легендарный",
}

# Заточка группируется ДИАПАЗОНАМИ, а не точным значением - иначе данные
# размазываются по 16 крошечным бакетам и почти для любого конкретного
# +N истории почти нет.
REFINEMENT_BANDS = [(0, 4), (5, 9), (10, 14), (15, 15)]


def refinement_band(ptn):
    for lo, hi in REFINEMENT_BANDS:
        if lo <= ptn <= hi:
            return (lo, hi)
    return (ptn, ptn)  # на случай значений за пределами ожидаемого диапазона


def refinement_band_label(band):
    lo, hi = band
    return f"+{lo}" if lo == hi else f"+{lo}..+{hi}"


# --------------------------------------------------------------------------
# STALCRAFT API client (минимум, только то, что нужно)
# --------------------------------------------------------------------------

session = requests.Session()
session.headers.update(HEADERS)


def api_get(path, params=None, max_retries=4):
    url = f"{BASE_URL}{path}"
    delay = 1.0
    for attempt in range(max_retries + 1):
        resp = session.get(url, params=params, timeout=15)
        if resp.status_code == 429:
            if attempt == max_retries:
                resp.raise_for_status()
            retry_after = resp.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else delay
            time.sleep(wait)
            delay *= 2  # экспоненциальный backoff, если Retry-After не пришёл
            continue
        resp.raise_for_status()
        return resp.json()


def get_auction_lots(item_id, limit=200):
    """Активные лоты по предмету (с buyout ценой).

    additional=true обязателен, иначе поле `additional` (в нём qlt -
    редкость и ptn - заточка) в ответе будет всегда пустым {}.
    """
    return api_get(
        f"/{REGION}/auction/{item_id}/lots",
        params={
            "limit": limit,
            "sort": "buyout_price",
            "order": "asc",
            "additional": "true",
        },
    )


def get_price_history(item_id, limit=None):
    """История продаж по предмету (тоже с разбивкой по редкости/заточке)."""
    return api_get(
        f"/{REGION}/auction/{item_id}/history",
        params={"limit": limit or HISTORY_FETCH_LIMIT, "additional": "true"},
    )


def extract_qlt_ptn(entry):
    """Достаём редкость (qlt) и заточку (ptn) из additional-блока.

    Если additional отсутствует или пуст (например для предмета без
    градаций) - считаем это "базовым" вариантом 0/0, а не выбрасываем
    запись, иначе для многих предметов пропадёт вся история.
    """
    additional = entry.get("additional") or {}
    qlt = additional.get("qlt", 0)
    ptn = additional.get("ptn", 0)
    return qlt, ptn


def parse_time(ts):
    """Парсим ISO-время вида 2024-01-07T15:47:54Z в aware datetime."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Локальное хранилище истории продаж (SQLite)
# --------------------------------------------------------------------------
# Официальный API отдаёт очень неглубокую историю (похоже, буквально
# пару дней вне зависимости от пагинации). Поэтому вместо того чтобы
# полагаться на глубину самого API, при каждом скане копим всё, что
# видим, в свой файл - со временем архив растёт и перестаёт зависеть
# от того, что покажет живой запрос прямо сейчас.

_db_conn = None
_db_lock = threading.Lock()


def init_db():
    global _db_conn
    _db_conn = sqlite3.connect(str(STORAGE_DB_PATH), check_same_thread=False)
    _db_conn.execute("PRAGMA journal_mode=WAL")
    _db_conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sales (
            item_id TEXT NOT NULL,
            qlt INTEGER NOT NULL,
            ptn INTEGER NOT NULL,
            price INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            sale_time TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            PRIMARY KEY (item_id, qlt, ptn, price, amount, sale_time)
        )
        """
    )
    _db_conn.execute("CREATE INDEX IF NOT EXISTS idx_sales_item ON sales(item_id)")
    # Если база создана предыдущей версией (без recorded_at) - добавляем
    # колонку и заполняем чем-то разумным, чтобы не падать на апгрейде.
    cols = [r[1] for r in _db_conn.execute("PRAGMA table_info(sales)").fetchall()]
    if "recorded_at" not in cols:
        _db_conn.execute("ALTER TABLE sales ADD COLUMN recorded_at TEXT")
        _db_conn.execute("UPDATE sales SET recorded_at = sale_time WHERE recorded_at IS NULL")
    _db_conn.commit()


def store_sales(item_id, raw_entries):
    """Кладём все увиденные продажи в локальную базу. INSERT OR IGNORE
    сам дедуплицирует - повторные сканы одних и тех же продаж не
    создают дублей (первичный ключ включает все значимые поля).

    recorded_at - это когда МЫ увидели продажу (момент скана), а
    sale_time - когда сама продажа произошла в игре. Это разные вещи:
    для низколиквидных предметов API может отдать давнюю продажу (раз
    новых мало, она не вытеснилась) - если бы мы ориентировались на
    sale_time для "с какого момента копим историю", дата скакала бы в
    прошлое каждый раз, когда попадается такая старая запись.
    """
    if not raw_entries:
        return 0

    now_iso = datetime.now(timezone.utc).isoformat()
    rows = []
    for e in raw_entries:
        price = e.get("price") or e.get("amount")
        sale_time = e.get("time")
        if not price or not sale_time:
            continue
        qlt, ptn = extract_qlt_ptn(e)
        amount = e.get("amount") or 1
        rows.append((item_id, qlt, ptn, price, amount, sale_time, now_iso))

    if not rows:
        return 0

    with _db_lock:
        before = _db_conn.total_changes
        _db_conn.executemany(
            "INSERT OR IGNORE INTO sales (item_id, qlt, ptn, price, amount, sale_time, recorded_at) VALUES (?,?,?,?,?,?,?)",
            rows,
        )
        _db_conn.commit()
        inserted = _db_conn.total_changes - before
    return inserted


def load_sales(item_id, qlt=None):
    """Читаем накопленную историю продаж предмета из локальной базы."""
    with _db_lock:
        if qlt is not None:
            cur = _db_conn.execute(
                "SELECT qlt, ptn, price, sale_time FROM sales WHERE item_id = ? AND qlt = ?",
                (item_id, qlt),
            )
        else:
            cur = _db_conn.execute(
                "SELECT qlt, ptn, price, sale_time FROM sales WHERE item_id = ?",
                (item_id,),
            )
        return cur.fetchall()


def db_collection_started_at():
    """Когда МЫ (этот инструмент) впервые начали писать в базу - не
    путать с датой самой старой продажи (sale_time), которая может быть
    давней для редких предметов."""
    with _db_lock:
        cur = _db_conn.execute("SELECT MIN(recorded_at) FROM sales")
        row = cur.fetchone()
    return row[0] if row and row[0] else None


def db_total_rows():
    with _db_lock:
        cur = _db_conn.execute("SELECT COUNT(*) FROM sales")
        return cur.fetchone()[0]


def normalize_rows(rows):
    """(qlt, ptn, price, sale_time) из базы -> удобные словари с
    распарсенным временем, готовые для bucket_reference_prices."""
    result = []
    for qlt, ptn, price, sale_time in rows:
        ts = parse_time(sale_time)
        if not ts:
            continue
        result.append({"qlt": qlt, "ptn": ptn, "price": price, "ts": ts})
    return result


def merge_external_db(external_path):
    """Сливаем продажи из ДРУГОГО файла базы (например, скачанного из
    облачного репозитория) в нашу локальную. INSERT OR IGNORE сам
    дедуплицирует пересекающиеся записи. Возвращает, сколько НОВЫХ
    строк реально добавилось."""
    external_path = str(external_path)
    ext_conn = sqlite3.connect(external_path)
    try:
        rows = ext_conn.execute(
            "SELECT item_id, qlt, ptn, price, amount, sale_time, recorded_at FROM sales"
        ).fetchall()
    finally:
        ext_conn.close()

    if not rows:
        return 0

    with _db_lock:
        before = _db_conn.total_changes
        _db_conn.executemany(
            "INSERT OR IGNORE INTO sales (item_id, qlt, ptn, price, amount, sale_time, recorded_at) VALUES (?,?,?,?,?,?,?)",
            rows,
        )
        _db_conn.commit()
        inserted = _db_conn.total_changes - before
    return inserted
