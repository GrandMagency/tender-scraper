#!/usr/bin/env python3
"""
tender_scraper.py — WAT Tool
Пошук тендерів на solar carport/PV через TED API (Tenders Electronic Daily, ЄС).
Фільтр: CPV-коди + ключові слова в назві, DACH (DE/AT/CH), scoring 0–100.

Вихід: output/tenders_<region>_<date>.csv
State:  .state.json (dedup між запусками)

Usage:
  python tender_scraper.py
  python tender_scraper.py --region DE
  python tender_scraper.py --region DACH --days 30
  python tender_scraper.py --min-score 50 --quiet
  python tender_scraper.py --output output/my_tenders.csv
  python tender_scraper.py --dry-run        # показати запит без виконання
"""

import argparse
import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

# ── TED API (v3, eForms) ───────────────────────────────────────────────────────
TED_SEARCH_URL = "https://api.ted.europa.eu/v3/notices/search"
REQUEST_TIMEOUT = 30   # секунди
RATE_LIMIT_SEC = 1.5   # пауза між сторінками
MAX_ITERATIONS = 100   # safety cap (~10,000 результатів)

# ── Відображення регіонів ──────────────────────────────────────────────────────
# TED використовує ISO 3166-1 alpha-3
REGION_TO_COUNTRIES = {
    "DE":   ["DEU"],
    "AT":   ["AUT"],
    "CH":   ["CHE"],
    "DACH": ["DEU", "AUT", "CHE"],
}

COUNTRY_ALPHA2 = {"DEU": "DE", "AUT": "AT", "CHE": "CH"}

# ── Ключові слова для solar carport ───────────────────────────────────────────
SOLAR_KEYWORDS = [
    "Solarcarport", "Solar-Carport", "Solar Carport",
    "PV-Carport", "PV Carport",
    "Photovoltaik Carport", "Photovoltaik-Carport",
    "Solardach Parkplatz", "Solardach-Parkplatz",
    "Carport photovoltaik", "Carport PV",
    "Überdachung Photovoltaik", "Parkplatz Solar",
]

# CPV-коди для solar/carport (префікси)
SOLAR_CPV_PREFIXES = {
    "09331200": "Solar photovoltaic modules",
    "09332000": "Solar installation",
    "45261215": "Solar panel roof-covering work",
    "44211000": "Prefabricated structures",
    "45223820": "Pre-fabricated units",
}

# ── TED API fields для запиту ──────────────────────────────────────────────────
TED_FIELDS = [
    "publication-number",     # ID тендера
    "notice-title",           # Назва (multilingual dict)
    "buyer-name",             # Замовник (list)
    "buyer-country",          # Країна (list, ISO alpha-3)
    "buyer-email",            # Email замовника (list)
    "organisation-email-buyer",  # Альтернативний email
    "publication-date",       # Дата публікації
    "deadline-date-lot",      # Дедлайн
    "estimated-value-lot",    # Бюджет (lot)
    "estimated-value-proc",   # Бюджет (procedure)
    "classification-cpv",     # CPV-коди (list)
    "notice-type",            # Тип тендера
]

# ── State-файл (dedup між запусками) ──────────────────────────────────────────
STATE_FILE = Path(".state.json")


# ── Dedup helpers ──────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"tenders": {}, "last_digest_run": None, "digest_tenders": {}}


def save_state(state: dict) -> None:
    """Атомний запис .state.json."""
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp.replace(STATE_FILE)


def make_tender_id(pub_number: str) -> str:
    """Унікальний ID з publication-number TED."""
    return pub_number.replace("/", "_").replace(" ", "_").replace("-", "_")


# ── Scoring (0–100) ────────────────────────────────────────────────────────────

def score_tender(cpv_codes: list[str], keywords_found: list[str],
                 value_eur: float | None, days_left: int | None) -> int:
    score = 0

    # CPV-match (0–40)
    matched_cpv = [c for c in cpv_codes if any(c.startswith(p) for p in SOLAR_CPV_PREFIXES)]
    if matched_cpv:
        score += 40
    elif any(c[:4] in ["0933", "4526", "4422", "4523"] for c in cpv_codes):
        score += 25

    # Keyword-match (0–30)
    kw_count = len(keywords_found)
    if kw_count >= 3:
        score += 30
    elif kw_count == 2:
        score += 20
    elif kw_count == 1:
        score += 10

    # Бюджет (0–20)
    if value_eur:
        if 50_000 <= value_eur <= 500_000:
            score += 20
        elif value_eur > 500_000:
            score += 10
        else:
            score += 5
    else:
        score += 5  # невідомий — мінімум

    # Дедлайн (0–10)
    if days_left is not None:
        if days_left <= 10:
            score += 10
        elif days_left <= 30:
            score += 5
        else:
            score += 2

    return min(score, 100)


# ── TED API ────────────────────────────────────────────────────────────────────

def build_query(countries: list[str], days: int) -> str:
    """
    Формує DSL query для TED Expert Search API.

    Синтаксис:
      field ~ "value"          — contains (для текстових полів)
      field IN (VAL1,VAL2)     — exact match з переліку
      field = VAL              — точний збіг
      publication-date >= today(-N)  — відносна дата (N днів назад)
      AND / OR / NOT           — логічні оператори
    """
    # Ключові слова в назві тендера
    kw_parts = " OR ".join(f'notice-title ~ "{kw}"' for kw in SOLAR_KEYWORDS)

    # CPV codes (direct match, без ~)
    cpv_parts = " OR ".join(
        f"classification-cpv IN ({code})"
        for code in SOLAR_CPV_PREFIXES
    )

    # Країни
    country_parts = " OR ".join(f"buyer-country IN ({c})" for c in countries)

    # Дата (TED підтримує today(-N) або YYYYMMDD)
    date_filter = f"publication-date >= today(-{days})"

    query = (
        f"({kw_parts} OR {cpv_parts})"
        f" AND ({country_parts})"
        f" AND {date_filter}"
    )
    return query


def build_payload(query: str, page: int = 1, page_size: int = 100) -> dict:
    return {
        "query": query,
        "fields": TED_FIELDS,
        "limit": page_size,
        "page": page,
    }


def fetch_page(payload: dict, verbose: bool = False) -> dict | None:
    """Один POST-запит до TED API. 3 спроби з backoff."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        TED_SEARCH_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "WAT-tender-scraper/1.0 (grandma.agency)",
        },
        method="POST",
    )
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                raw = resp.read().decode("utf-8")
            return json.loads(raw)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")
            if verbose:
                print(f"  HTTP {e.code}: {err_body[:300]}")
            return None
        except Exception as e:
            if verbose:
                print(f"  Спроба {attempt}/3: {e}")
            if attempt < 3:
                time.sleep(attempt * 2)
    return None


def search_ted(countries: list[str], days: int, verbose: bool = True):
    """
    Генератор: пагінує TED API (page-based) і повертає notices по одному.
    """
    query = build_query(countries, days)
    page = 1
    total_seen = 0
    page_size = 100

    while page <= MAX_ITERATIONS:
        payload = build_payload(query, page=page, page_size=page_size)

        if verbose:
            print(f"  Сторінка {page} (отримано: {total_seen})...", end=" ", flush=True)

        data = fetch_page(payload, verbose=True)

        if data is None:
            if verbose:
                print("ПОМИЛКА — зупиняємо")
            break

        notices = data.get("notices", [])
        total = data.get("totalNoticeCount", "?")

        if not notices:
            if verbose:
                print("0 — кінець")
            break

        if verbose:
            print(f"{len(notices)} (всього: {total})")

        for notice in notices:
            yield notice
            total_seen += 1

        # Якщо отримали менше ніж page_size — остання сторінка
        if len(notices) < page_size:
            break

        page += 1
        time.sleep(RATE_LIMIT_SEC)


# ── Обробка notice ─────────────────────────────────────────────────────────────

def get_multilingual(val, preferred_langs=("deu", "eng")) -> str:
    """Витягує текст з multilingual dict.
    Обробляє: str, dict{lang: str}, dict{lang: [str, ...]}, list[str].
    """
    if not val:
        return ""
    if isinstance(val, str):
        return val[:300]
    if isinstance(val, list):
        # Список рядків — беремо перший
        item = val[0] if val else ""
        return str(item)[:300] if not isinstance(item, (list, dict)) else get_multilingual(item, preferred_langs)
    if isinstance(val, dict):
        for lang in preferred_langs:
            if lang in val:
                v = val[lang]
                # Значення може бути списком ["Назва"] або рядком "Назва"
                if isinstance(v, list):
                    return str(v[0])[:300] if v else ""
                return str(v)[:300]
        # Не знайшли preferred — беремо будь-яке
        first_v = next(iter(val.values()), "")
        if isinstance(first_v, list):
            return str(first_v[0])[:300] if first_v else ""
        return str(first_v)[:300]
    return str(val)[:300]


def get_list_first(val) -> str:
    """Повертає перший елемент списку або рядок."""
    if not val:
        return ""
    if isinstance(val, list):
        return str(val[0]) if val else ""
    return str(val)


def parse_date(val) -> str:
    """Нормалізує дату в YYYY-MM-DD."""
    if not val:
        return ""
    s = str(val)[:10]  # Відрізаємо час і timezone
    return s.replace("T", "")[:10]


def extract_value(notice: dict) -> float | None:
    """Витягує estimated value в EUR."""
    for key in ["estimated-value-lot", "estimated-value-proc", "estimated-value-glo"]:
        v = notice.get(key)
        if v is None:
            continue
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, list) and v:
            # Може бути список об'єктів {amount, currency}
            first = v[0]
            if isinstance(first, (int, float)):
                return float(first)
            if isinstance(first, dict):
                amt = first.get("amount") or first.get("value")
                if amt:
                    return float(amt)
        if isinstance(v, dict):
            amt = v.get("amount") or v.get("value")
            if amt:
                return float(amt)
        if isinstance(v, str):
            try:
                return float(v.replace(",", "").replace(" ", ""))
            except ValueError:
                pass
    return None


def extract_cpv_codes(notice: dict) -> list[str]:
    """Витягує список CPV-кодів."""
    raw = notice.get("classification-cpv", [])
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        # Дедупліковати
        seen = set()
        result = []
        for item in raw:
            code = str(item).strip() if not isinstance(item, dict) else item.get("code", "")
            if code and code not in seen:
                seen.add(code)
                result.append(code)
        return result
    return []


def find_keywords(notice: dict) -> list[str]:
    """Шукає SOLAR_KEYWORDS у тексті notice."""
    title = notice.get("notice-title", "")
    text = get_multilingual(title, ("deu", "eng")).lower()
    return [kw for kw in SOLAR_KEYWORDS if kw.lower() in text]


def ted_notice_url(pub_number: str) -> str:
    """URL на сторінку тендера."""
    return f"https://ted.europa.eu/en/notice/{pub_number}"


def process_notice(notice: dict, seen_ids: set, state: dict,
                   min_score: int) -> dict | None:
    today = date.today()

    pub_number = notice.get("publication-number", "")
    if not pub_number:
        return None

    tender_id = make_tender_id(str(pub_number))

    # Dedup
    if tender_id in state["tenders"] or tender_id in seen_ids:
        return None

    # Дати
    pub_date = parse_date(notice.get("publication-date", ""))

    deadline_raw = notice.get("deadline-date-lot", "")
    if isinstance(deadline_raw, list):
        deadline_raw = deadline_raw[0] if deadline_raw else ""
    deadline_date = parse_date(deadline_raw)

    days_left = None
    if deadline_date:
        try:
            dl = datetime.strptime(deadline_date[:10], "%Y-%m-%d").date()
            days_left = (dl - today).days
        except ValueError:
            pass

    # CPV
    cpv_codes = extract_cpv_codes(notice)

    # Keywords
    keywords_found = find_keywords(notice)

    # Якщо нічого релевантного — пропустити
    if not cpv_codes and not keywords_found:
        return None

    # Бюджет
    value_eur = extract_value(notice)

    # Score
    priority_score = score_tender(cpv_codes, keywords_found, value_eur, days_left)

    if priority_score < min_score:
        return None

    # Buyer
    buyer_name = get_multilingual(
        notice.get("buyer-name", notice.get("organisation-name-buyer", ""))
    )
    buyer_country_raw = notice.get("buyer-country", "")
    buyer_country_alpha3 = get_list_first(buyer_country_raw)
    buyer_country = COUNTRY_ALPHA2.get(buyer_country_alpha3, buyer_country_alpha3[:2])

    buyer_email = get_list_first(
        notice.get("buyer-email") or notice.get("organisation-email-buyer", "")
    )

    # Title
    title = get_multilingual(notice.get("notice-title", ""), ("deu", "eng"))

    return {
        "tender_id": tender_id,
        "publication_number": str(pub_number),
        "title": title,
        "buyer_name": buyer_name,
        "buyer_country": buyer_country,
        "buyer_email": buyer_email,
        "cpv_codes": ",".join(cpv_codes),
        "keyword_match": ",".join(keywords_found),
        "estimated_value_eur": value_eur or "",
        "deadline_date": deadline_date,
        "days_until_deadline": days_left if days_left is not None else "",
        "publication_date": pub_date,
        "priority_score": priority_score,
        "ted_url": ted_notice_url(str(pub_number)),
    }


# ── Telegram ───────────────────────────────────────────────────────────────────

def tg_notify(msg: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_USER_ID")
    if not token or not chat_id:
        return
    try:
        body = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": msg,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=body,
            headers={"User-Agent": "WAT-tender-scraper/1.0"},
        )
        urllib.request.urlopen(req, timeout=8)
    except Exception:
        pass


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Пошук тендерів на solar carport через TED API (EU Public Procurement)"
    )
    parser.add_argument(
        "--region",
        default="DACH",
        choices=["DE", "AT", "CH", "DACH"],
        help="Регіон: DE, AT, CH або DACH (всі три). Default: DACH",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Кількість днів назад для пошуку (default: 7)",
    )
    parser.add_argument(
        "--min-score",
        type=int,
        default=30,
        help="Мінімальний пріоритет 0–100 (default: 30)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Шлях до CSV (default: output/tenders_<region>_<date>.csv)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Без verbose виводу",
    )
    parser.add_argument(
        "--no-telegram",
        action="store_true",
        help="Пропустити Telegram повідомлення",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Показати запит без виконання",
    )
    args = parser.parse_args()

    verbose = not args.quiet
    region = args.region
    today = date.today()

    countries = REGION_TO_COUNTRIES[region]

    output_path = Path(args.output) if args.output else Path(
        f"output/tenders_{region.lower()}_{today}.csv"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if verbose:
        print(f"\n📋 Tender Scraper — {region}")
        print(f"   Регіони: {', '.join(countries)}")
        print(f"   Останні {args.days} днів")
        print(f"   Мін. score: {args.min_score}")
        print(f"   Вихід: {output_path}\n")

    if args.dry_run:
        query = build_query(countries, args.days)
        payload = build_payload(query)
        print("── Dry-run: запит до TED API ─────────────────────────")
        print(f"  URL: {TED_SEARCH_URL}")
        print(f"  Body:\n{json.dumps(payload, ensure_ascii=False, indent=2)}")
        print("──────────────────────────────────────────────────────")
        sys.exit(0)

    # Стан dedup
    state = load_state()
    seen_ids: set = set()

    # Streaming CSV
    csv_file = None
    writer = None
    total_records = 0
    t0 = time.time()

    def ensure_writer(sample: dict):
        nonlocal csv_file, writer
        if writer is None:
            csv_file = open(output_path, "w", newline="", encoding="utf-8")
            writer = csv.DictWriter(csv_file, fieldnames=list(sample.keys()))
            writer.writeheader()

    for notice in search_ted(countries, args.days, verbose=verbose):
        record = process_notice(notice, seen_ids, state, args.min_score)
        if record is None:
            continue

        ensure_writer(record)
        writer.writerow(record)
        csv_file.flush()

        seen_ids.add(record["tender_id"])
        state["tenders"][record["tender_id"]] = record["publication_number"]
        total_records += 1

        if verbose and total_records % 10 == 0:
            print(f"  ✓ {total_records} тендерів записано...")

    if csv_file:
        csv_file.close()

    save_state(state)

    elapsed = time.time() - t0

    if verbose:
        print(f"\n  Результат: {total_records} тендерів (score ≥ {args.min_score})")
        print(f"  Час виконання: {elapsed:.1f} сек")
        if total_records:
            print(f"✓ Збережено → {output_path}")
        else:
            print("  Нічого не знайдено. Спробуй: --min-score 0 або --days 90")

    if not args.no_telegram:
        if total_records > 0:
            msg = (
                f"📋 <b>Tender Scraper — ГОТОВО</b>\n\n"
                f"🌍 Регіон: {region}\n"
                f"🏁 Тендерів: <b>{total_records}</b> (score ≥ {args.min_score})\n"
                f"📁 Файл: {output_path}\n"
                f"⏱ Час: {elapsed:.0f} сек"
            )
        else:
            msg = (
                f"📋 <b>Tender Scraper</b> — нових тендерів не знайдено\n"
                f"🌍 {region} | останні {args.days} днів"
            )
        tg_notify(msg)


if __name__ == "__main__":
    main()
