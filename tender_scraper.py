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
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
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
# TED API query: оператор ~ шукає підрядок у notice-title
# Перевірено: "Solarcarport", "Carport" (з AND Solar), "Parkplatz" (з AND PV) — дають результати

# Група 1: явний "Carport" в назві
CARPORT_KEYWORDS = [
    "Solarcarport", "Solar-Carport",
    "PV-Carport",
    "Carport",          # буде зʼєднано AND з PV у query
]

# Група 2: паркінг + PV (Parkplatz + Photovoltaik/Solar/PV)
PARKING_PV_TERMS = [
    "Parkplatz", "Parkhaus", "Stellplatz", "Parkdeck",
    "Parkplatzüberdachung",   # дуже специфічний — уже означає "накриття паркінгу"
    "Überdachung",            # "дах/навіс" — буде AND Solar у query
]
SOLAR_TERMS      = ["Photovoltaik", "Solar", "PV-Anlage"]

# Для keyword_match та scoring: всі ключові слова разом
SOLAR_KEYWORDS = CARPORT_KEYWORDS + PARKING_PV_TERMS + SOLAR_TERMS

# CPV тільки для скорингу (не для query)
SOLAR_CPV_PREFIXES = {
    "09331200": "Solar photovoltaic modules",
    "09332000": "Solar installation",
    "45261215": "Solar panel roof-covering work",
}

# bund.de: ті самі групи
BUND_KEYWORDS = SOLAR_KEYWORDS

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

# ── Bund.de RSS ───────────────────────────────────────────────────────────────
BUND_RSS_URL = (
    "https://www.service.bund.de/Content/Globals/Functions/"
    "RSSFeed/RSSGenerator_Ausschreibungen.xml"
)

# ── simap.ch (Швейцарія) ────────────────────────────────────────────────────────
SIMAP_SEARCH_BASE  = "https://www.simap.ch/shabforms/COMMON/search/searchAtom.html"
SIMAP_SEARCH_TERMS = [
    "Solarcarport", "Solar Carport", "PV Carport",
    "Carport Photovoltaik",
    "Parkplatz Photovoltaik", "Parkhaus Solar",
]

# ── vergabe.nrw.de (NRW, Німеччина) ────────────────────────────────────────────
VERGABE_NRW_RSS = "https://www.vergabe.nrw.de/rss.xml"

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
    # Група 1: явні терміни в будь-якому полі (FT) — найгарячіші ліди
    ft_carport = " OR ".join(f'FT ~ "{kw}"' for kw in [
        "Solarcarport", "Solar-Carport", "PV-Carport",
        "Carport Photovoltaik", "Photovoltaik Carport",
        "Solar Carport",
        "Parkplatzüberdachung",    # дуже специфічний термін
    ])

    # Група 2: Carport + Solar в НАЗВІ тендера
    title_carport = " OR ".join(f'notice-title ~ "{kw}"' for kw in CARPORT_KEYWORDS)
    title_solar   = " OR ".join(f'notice-title ~ "{t}"' for t in SOLAR_TERMS)
    title_carport_pv = f"({title_carport}) AND ({title_solar})"

    # Група 3: Паркінг/Дах + PV в НАЗВІ (термін AND Photovoltaik/Solar)
    # Включає: Parkplatz, Parkplatzüberdachung, Überdachung + Solar
    title_parking    = " OR ".join(f'notice-title ~ "{t}"' for t in PARKING_PV_TERMS)
    title_parking_pv = f"({title_parking}) AND ({title_solar})"

    # Країни і дата
    country_parts = " OR ".join(f"buyer-country IN ({c})" for c in countries)
    date_filter   = f"publication-date >= today(-{days})"

    query = (
        f"({ft_carport} OR ({title_carport_pv}) OR ({title_parking_pv}))"
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
    """Шукає SOLAR_KEYWORDS у назві notice."""
    title = get_multilingual(notice.get("notice-title", ""), ("deu", "eng"))
    return [kw for kw in SOLAR_KEYWORDS if kw.lower() in title.lower()]


def ted_notice_url(pub_number: str) -> str:
    """URL на сторінку тендера."""
    return f"https://ted.europa.eu/en/notice/-/detail/{pub_number}"


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

    # TED query вже відфільтрував релевантні тендери (FT + title)
    # keywords_found може бути пустим якщо match був у тілі документа (FT)
    if not keywords_found and not cpv_codes:
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
        "source": "TED",
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


# ── Bund.de RSS scraper ────────────────────────────────────────────────────────

def _strip_html(text: str) -> str:
    """Видаляє HTML-теги і декодує базові entities."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">") \
               .replace("&nbsp;", " ").replace("&#39;", "'").replace("&quot;", '"')
    return re.sub(r"\s+", " ", text).strip()


def _parse_bund_description(html: str) -> dict:
    """
    Витягує структуровані поля з HTML-опису bund.de RSS item.
    Поля: Vergabestelle (buyer), Angebotsfrist/Bewerbungsfrist (deadline), Erfüllungsort (location).
    """
    result: dict = {}
    plain = _strip_html(html)
    # Кожен рядок HTML виглядає як: <p><strong>Vergabestelle:</strong> Назва</p>
    for field, labels in [
        ("buyer",    ["Vergabestelle", "Auftraggeber", "Auftraggeber/in"]),
        ("deadline", ["Angebotsfrist", "Bewerbungsfrist", "Einreichungsfrist"]),
        ("location", ["Erfüllungsort", "Leistungsort"]),
    ]:
        for label in labels:
            # Шукаємо в plain тексті: "Label: Значення"
            m = re.search(
                rf"{re.escape(label)}\s*:\s*(.+?)(?=\s{3,}|\Z|[A-Z][a-z]+\s*:)",
                plain,
            )
            if m:
                result[field] = m.group(1).strip().rstrip(".")
                break
    return result


def _parse_bund_date(raw: str) -> tuple[str, int | None]:
    """
    Парсить дату з bund.de (формат DD.MM.YYYY або DD.MM.YYYY HH:MM).
    Повертає (YYYY-MM-DD, days_left) або ("", None).
    """
    if not raw:
        return "", None
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", raw)
    if not m:
        return "", None
    iso = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    try:
        dl = datetime.strptime(iso, "%Y-%m-%d").date()
        days_left = (dl - date.today()).days
        return iso, days_left
    except ValueError:
        return iso, None


def _bund_tender_id(guid: str) -> str:
    """Stable dedup ID з bund.de GUID."""
    slug = guid.rstrip("/").split("/")[-1].replace(".html", "")
    return f"bund_{slug}" if slug else f"bund_{abs(hash(guid)) % 10**10}"


def _extract_deadline_from_text(text: str) -> str:
    """Знаходить першу дату DD.MM.YYYY в тексті."""
    m = re.search(r"\d{2}\.\d{2}\.\d{4}", text)
    return m.group(0) if m else ""


def search_bund_de(days: int, verbose: bool = True):
    """
    Generator: завантажує bund.de RSS і повертає items що містять SOLAR_KEYWORDS.
    """
    if verbose:
        print(f"\n[Bund.de] Завантаження RSS ({days}д)...")

    req = urllib.request.Request(
        BUND_RSS_URL,
        headers={"User-Agent": "WAT-tender-scraper/1.0 (grandma.agency)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
    except Exception as e:
        if verbose:
            print(f"  Bund.de fetch error: {e}")
        return

    try:
        root = ET.fromstring(raw)
    except Exception as e:
        if verbose:
            print(f"  Bund.de XML parse error: {e}")
        return

    cutoff = date.today() - timedelta(days=days)
    items = root.findall(".//item")

    if verbose:
        print(f"  {len(items)} items у фіді, фільтруємо за {days}д + keywords...")

    yielded = 0
    for item in items:
        # Перевірка дати публікації
        pub_raw = item.findtext("pubDate", "")
        pub_dt: date | None = None
        try:
            pub_dt = parsedate_to_datetime(pub_raw).date()
            if pub_dt < cutoff:
                continue
        except Exception:
            pass  # без дати — включаємо

        title       = (item.findtext("title") or "").strip()
        link        = (item.findtext("link") or "").strip()
        description = (item.findtext("description") or "").strip()
        guid        = (item.findtext("guid") or link).strip()

        # Фільтр: або явний carport, або parking + solar (AND логіка як у TED)
        search_text = (title + " " + _strip_html(description)).lower()

        has_carport = any(kw.lower() in search_text for kw in CARPORT_KEYWORDS)
        has_parking = any(t.lower() in search_text for t in PARKING_PV_TERMS)
        has_solar   = any(t.lower() in search_text for t in SOLAR_TERMS)

        if not (has_carport or (has_parking and has_solar)):
            continue

        keywords_found = [kw for kw in BUND_KEYWORDS if kw.lower() in search_text]

        yielded += 1
        yield {
            "title":          title,
            "link":           link,
            "description":    description,
            "guid":           guid,
            "pub_dt":         pub_dt or date.today(),
            "keywords_found": keywords_found,
        }

    if verbose:
        print(f"  Bund.de: {yielded} релевантних items")


def process_bund_item(item: dict, seen_ids: set, state: dict,
                      min_score: int) -> dict | None:
    """Нормалізує bund.de RSS item у стандартний tender record."""
    tender_id = _bund_tender_id(item["guid"])

    if tender_id in state.get("tenders", {}) or tender_id in seen_ids:
        return None

    desc_fields  = _parse_bund_description(item["description"])
    buyer_name   = desc_fields.get("buyer", "")
    deadline_iso, days_left = _parse_bund_date(desc_fields.get("deadline", ""))

    # Score: тільки keywords і дедлайн (CPV і бюджет недоступні з RSS)
    # Базовий бонус 25 замість CPV — bund.de вже відфільтровано за solar keywords
    score = 25 + score_tender([], item["keywords_found"], None, days_left)
    score = min(score, 100)
    if score < min_score:
        return None

    pub_number = item["guid"].rstrip("/").split("/")[-1].replace(".html", "") or tender_id

    return {
        "tender_id":           tender_id,
        "publication_number":  pub_number,
        "title":               item["title"][:300],
        "buyer_name":          buyer_name,
        "buyer_country":       "DE",
        "buyer_email":         "",
        "cpv_codes":           "",
        "keyword_match":       ",".join(item["keywords_found"]),
        "estimated_value_eur": "",
        "deadline_date":       deadline_iso,
        "days_until_deadline": days_left if days_left is not None else "",
        "publication_date":    item["pub_dt"].isoformat(),
        "priority_score":      score,
        "ted_url":             item["link"],
        "source":              "Bund.de",
    }


# ── simap.ch (Швейцарія) ───────────────────────────────────────────────────────

def search_simap_ch(days: int, verbose: bool = True):
    """
    Generator: пошук тендерів на simap.ch за ключовими словами.
    Робить окремий запит для кожного search term, дедуплікує по entry ID.
    """
    if verbose:
        print(f"\n[simap.ch] Пошук ({days}д, {len(SIMAP_SEARCH_TERMS)} terms)...")

    cutoff       = date.today() - timedelta(days=days)
    seen_guids: set[str] = set()
    yielded      = 0
    NS           = "http://www.w3.org/2005/Atom"

    for term in SIMAP_SEARCH_TERMS:
        params = urllib.parse.urlencode({"LANG": "de", "NOTICE_TITLE": term})
        url    = f"{SIMAP_SEARCH_BASE}?{params}"
        req    = urllib.request.Request(
            url, headers={"User-Agent": "WAT-tender-scraper/1.0 (grandma.agency)"}
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
        except Exception as e:
            if verbose:
                print(f"  [{term}] помилка: {e}")
            continue

        try:
            root = ET.fromstring(raw)
        except Exception as e:
            if verbose:
                print(f"  [{term}] XML parse error: {e}")
            continue

        # Atom entries (з namespace або без)
        entries = root.findall(f"{{{NS}}}entry") or root.findall(".//entry")

        for entry in entries:
            def _at(tag: str) -> str:
                return (
                    entry.findtext(f"{{{NS}}}{tag}") or
                    entry.findtext(tag) or ""
                )

            guid = _at("id")
            if not guid or guid in seen_guids:
                continue
            seen_guids.add(guid)

            updated = _at("updated") or _at("published")
            pub_dt: date | None = None
            try:
                pub_dt = datetime.fromisoformat(updated[:10]).date()
                if pub_dt < cutoff:
                    continue
            except (ValueError, TypeError):
                pass

            title    = _at("title")
            link_el  = entry.find(f"{{{NS}}}link") or entry.find("link")
            link     = link_el.get("href", "") if link_el is not None else ""
            summary  = _at("summary") or _at("content")

            search_text = (title + " " + _strip_html(summary)).lower()
            has_carport = any(kw.lower() in search_text for kw in CARPORT_KEYWORDS)
            has_parking = any(t.lower()  in search_text for t  in PARKING_PV_TERMS)
            has_solar   = any(t.lower()  in search_text for t  in SOLAR_TERMS)

            if not (has_carport or (has_parking and has_solar)):
                continue

            keywords_found = [kw for kw in SOLAR_KEYWORDS if kw.lower() in search_text]
            yielded += 1
            yield {
                "title":          title[:300],
                "link":           link,
                "description":    summary,
                "guid":           guid,
                "pub_dt":         pub_dt or date.today(),
                "keywords_found": keywords_found,
            }

        time.sleep(0.5)

    if verbose:
        print(f"  simap.ch: {yielded} релевантних тендерів")


def process_simap_item(item: dict, seen_ids: set, state: dict,
                       min_score: int) -> dict | None:
    """Нормалізує simap.ch Atom entry у стандартний tender record."""
    slug      = item["guid"].rstrip("/").split("/")[-1].split("?")[0].replace(".html", "")
    tender_id = f"simap_{slug}" if slug else f"simap_{abs(hash(item['guid'])) % 10**10}"

    if tender_id in state.get("tenders", {}) or tender_id in seen_ids:
        return None

    desc_plain   = _strip_html(item.get("description", ""))
    desc_fields  = _parse_bund_description(item.get("description", ""))
    buyer_name   = desc_fields.get("buyer", "")
    deadline_raw = _extract_deadline_from_text(desc_plain)
    deadline_iso, days_left = _parse_bund_date(deadline_raw)

    score = 25 + score_tender([], item["keywords_found"], None, days_left)
    score = min(score, 100)
    if score < min_score:
        return None

    return {
        "tender_id":           tender_id,
        "publication_number":  slug or tender_id,
        "title":               item["title"][:300],
        "buyer_name":          buyer_name,
        "buyer_country":       "CH",
        "buyer_email":         "",
        "cpv_codes":           "",
        "keyword_match":       ",".join(item["keywords_found"]),
        "estimated_value_eur": "",
        "deadline_date":       deadline_iso,
        "days_until_deadline": days_left if days_left is not None else "",
        "publication_date":    item["pub_dt"].isoformat(),
        "priority_score":      score,
        "ted_url":             item["link"],
        "source":              "simap.ch",
    }


# ── vergabe.nrw.de (NRW, найбільша земля DE) ───────────────────────────────────

def search_vergabe_nrw(days: int, verbose: bool = True):
    """
    Generator: завантажує vergabe.nrw.de RSS і фільтрує за keywords (AND-логіка).
    RSS містить останні ~100 оголошень; для historical > 30д результати обмежені.
    """
    if verbose:
        print(f"\n[vergabe.nrw.de] Завантаження RSS ({days}д)...")

    req = urllib.request.Request(
        VERGABE_NRW_RSS,
        headers={"User-Agent": "WAT-tender-scraper/1.0 (grandma.agency)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
    except Exception as e:
        if verbose:
            print(f"  vergabe.nrw.de fetch error: {e}")
        return

    try:
        root = ET.fromstring(raw)
    except Exception as e:
        if verbose:
            print(f"  vergabe.nrw.de XML parse error: {e}")
        return

    cutoff = date.today() - timedelta(days=days)
    items  = root.findall(".//item")

    if verbose:
        print(f"  {len(items)} items у фіді, фільтруємо...")

    yielded = 0
    for item in items:
        pub_raw = item.findtext("pubDate", "")
        pub_dt: date | None = None
        try:
            pub_dt = parsedate_to_datetime(pub_raw).date()
            if pub_dt < cutoff:
                continue
        except Exception:
            pass

        title       = (item.findtext("title") or "").strip()
        link        = (item.findtext("link") or "").strip()
        description = (item.findtext("description") or "").strip()
        guid        = (item.findtext("guid") or link).strip()

        search_text = (title + " " + _strip_html(description)).lower()
        has_carport = any(kw.lower() in search_text for kw in CARPORT_KEYWORDS)
        has_parking = any(t.lower()  in search_text for t  in PARKING_PV_TERMS)
        has_solar   = any(t.lower()  in search_text for t  in SOLAR_TERMS)

        if not (has_carport or (has_parking and has_solar)):
            continue

        keywords_found = [kw for kw in SOLAR_KEYWORDS if kw.lower() in search_text]
        yielded += 1
        yield {
            "title":          title[:300],
            "link":           link,
            "description":    description,
            "guid":           guid,
            "pub_dt":         pub_dt or date.today(),
            "keywords_found": keywords_found,
        }

    if verbose:
        print(f"  vergabe.nrw.de: {yielded} релевантних items")


def process_vergabe_item(item: dict, seen_ids: set, state: dict,
                         min_score: int) -> dict | None:
    """Нормалізує vergabe.nrw.de RSS item у стандартний tender record."""
    slug      = item["guid"].rstrip("/").split("/")[-1].split("?")[0]
    tender_id = f"nrw_{slug}" if slug else f"nrw_{abs(hash(item['guid'])) % 10**10}"

    if tender_id in state.get("tenders", {}) or tender_id in seen_ids:
        return None

    desc_plain   = _strip_html(item["description"])
    desc_fields  = _parse_bund_description(item["description"])
    buyer_name   = desc_fields.get("buyer", "")
    deadline_raw = _extract_deadline_from_text(desc_plain)
    deadline_iso, days_left = _parse_bund_date(deadline_raw)

    score = 25 + score_tender([], item["keywords_found"], None, days_left)
    score = min(score, 100)
    if score < min_score:
        return None

    return {
        "tender_id":           tender_id,
        "publication_number":  slug or tender_id,
        "title":               item["title"][:300],
        "buyer_name":          buyer_name,
        "buyer_country":       "DE",
        "buyer_email":         "",
        "cpv_codes":           "",
        "keyword_match":       ",".join(item["keywords_found"]),
        "estimated_value_eur": "",
        "deadline_date":       deadline_iso,
        "days_until_deadline": days_left if days_left is not None else "",
        "publication_date":    item["pub_dt"].isoformat(),
        "priority_score":      score,
        "ted_url":             item["link"],
        "source":              "vergabe.nrw",
    }


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
        default=365,
        help="Кількість днів назад для пошуку (default: 365)",
    )
    parser.add_argument(
        "--min-score",
        type=int,
        default=20,
        help="Мінімальний пріоритет 0–100 (default: 20)",
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
    parser.add_argument(
        "--no-dedup",
        action="store_true",
        help="Повернути всі тендери без перевірки state (для digest)",
    )
    parser.add_argument(
        "--source",
        default="all",
        choices=["all", "ted", "bund", "simap", "nrw"],
        help="Джерело: all (TED+Bund.de+simap.ch+vergabe.nrw), ted, bund, simap, nrw (default: all)",
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

    # Які джерела активні для цього регіону
    active_sources: list[str] = []
    if args.source in ("all", "ted"):
        active_sources.append("TED")
    if args.source in ("all", "bund") and region in ("DE", "DACH"):
        active_sources.append("Bund.de")
    if args.source in ("all", "simap") and region in ("CH", "DACH"):
        active_sources.append("simap.ch")
    if args.source in ("all", "nrw") and region in ("DE", "DACH"):
        active_sources.append("vergabe.nrw")

    if verbose:
        print(f"\n📋 Tender Scraper — {region}")
        print(f"   Регіони: {', '.join(countries)}")
        print(f"   Джерела: {', '.join(active_sources)}")
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

    dedup_state = {"tenders": {}} if args.no_dedup else state

    # ── TED ──────────────────────────────────────────────────────────────────────
    if args.source in ("all", "ted"):
        if verbose:
            print("[TED] Пошук тендерів...")
        for notice in search_ted(countries, args.days, verbose=verbose):
            record = process_notice(notice, seen_ids, dedup_state, args.min_score)
            if record is None:
                continue
            ensure_writer(record)
            writer.writerow(record)
            csv_file.flush()
            seen_ids.add(record["tender_id"])
            if not args.no_dedup:
                state["tenders"][record["tender_id"]] = record["publication_number"]
            total_records += 1
            if verbose and total_records % 10 == 0:
                print(f"  ✓ {total_records} тендерів записано...")

    # ── Bund.de (тільки для DE або DACH) ─────────────────────────────────────────
    if args.source in ("all", "bund") and region in ("DE", "DACH"):
        for item in search_bund_de(args.days, verbose=verbose):
            record = process_bund_item(item, seen_ids, dedup_state, args.min_score)
            if record is None:
                continue
            ensure_writer(record)
            writer.writerow(record)
            csv_file.flush()
            seen_ids.add(record["tender_id"])
            if not args.no_dedup:
                state["tenders"][record["tender_id"]] = record["publication_number"]
            total_records += 1
            if verbose and total_records % 10 == 0:
                print(f"  ✓ {total_records} тендерів записано...")

    # ── simap.ch (тільки для CH або DACH) ────────────────────────────────────────
    if args.source in ("all", "simap") and region in ("CH", "DACH"):
        for item in search_simap_ch(args.days, verbose=verbose):
            record = process_simap_item(item, seen_ids, dedup_state, args.min_score)
            if record is None:
                continue
            ensure_writer(record)
            writer.writerow(record)
            csv_file.flush()
            seen_ids.add(record["tender_id"])
            if not args.no_dedup:
                state["tenders"][record["tender_id"]] = record["publication_number"]
            total_records += 1
            if verbose and total_records % 10 == 0:
                print(f"  ✓ {total_records} тендерів записано...")

    # ── vergabe.nrw.de (тільки для DE або DACH) ──────────────────────────────────
    if args.source in ("all", "nrw") and region in ("DE", "DACH"):
        for item in search_vergabe_nrw(args.days, verbose=verbose):
            record = process_vergabe_item(item, seen_ids, dedup_state, args.min_score)
            if record is None:
                continue
            ensure_writer(record)
            writer.writerow(record)
            csv_file.flush()
            seen_ids.add(record["tender_id"])
            if not args.no_dedup:
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
