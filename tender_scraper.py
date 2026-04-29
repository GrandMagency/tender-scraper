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

# Група 1: явний "Carport" або специфічний parking-solar термін
CARPORT_KEYWORDS = [
    "Solarcarport", "Solar-Carport",
    "PV-Carport",
    "Carport-PV-Anlage",                  # Photovoltaikanlage auf Carport
    "Photovoltaik-Parkplatzüberdachung",  # compound form used by many municipalities
    "Carport",               # буде зʼєднано AND з PV у query
    "Parkplatzüberdachung",  # "накриття паркінгу" — настільки специфічний, що AND Solar не треба
]

# Група 2: паркінг + PV (Parkplatz + Photovoltaik/Solar/PV)
PARKING_PV_TERMS = [
    "Parkplatz", "Parkhaus", "Stellplatz", "Parkdeck",
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
    "45213312": "Single storey car parks",
    "45213200": "Parking facilities",
    "45223200": "Structural work",
    "45310000": "Electrical installation work",
    "45315700": "Switching station fitting",
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

# ── simap.ch (Швейцарія) — REST API (знайдено через DevTools 2026-04-21) ────────
SIMAP_API_URL = "https://www.simap.ch/rest/publications/v2/project/project-search"
# Примітка: "solarcarport" (1 слово) → 0 результатів у базі; "solar carport" (2 слова) → є
SIMAP_SEARCH_TERMS = [
    "solar carport",
    "carport",
    "photovoltaik carport",
    "parkplatz photovoltaik",
    "parkhaus solar",
    "überdachung photovoltaik",
    "carport pv anlage",  # simap needs space-separated terms
]

# ── vergabe.nrw.de (NRW, Німеччина) ────────────────────────────────────────────
VERGABE_NRW_RSS = "https://www.vergabe.nrw.de/rss.xml"

# ── oeffentlichevergabe.de (Bekanntmachungsservice — Deutschland) ─────────────
# OpenData API: daily ZIP export (notice.csv + purpose.csv + organisation.csv + classification.csv)
# Free, no auth. ~80–150 KB/day, ~120–200 notices/day (all German federal/state notices).
# Джерела покрити: service.bund.de/eVergabe (federal) + частина земель через Bekanntmachungsservice
OEFFENTLICH_API_URL = "https://oeffentlichevergabe.de/api/notice-exports"
# Максимальна кількість днів для bulk download (щоб не перевантажити API)
OEFFENTLICH_MAX_DAYS = 30

# ── open.nrw (NRW OpenData CKAN) ───────────────────────────────────────────────
# Open.NRW publishes state procurement tenders as a public CKAN dataset
OPEN_NRW_CKAN_URL = "https://open.nrw/api/3/action/package_show"
OPEN_NRW_PACKAGE_ID = "ausschreibungen_des_vergabemarktplatzes_nrw_1587477165"

# Підписки / закриті портали — не реалізуємо:
# ibau, subreport ELViS, B_I MEDIEN, Vergabe24, TenderRadar, TenderPipe,
# TendersOnTime, DTVP (тільки email-нотифікація), evergabe.de (реєстрація)

# ── bescha.bund.de (Federal Procurement Office) ────────────────────────────────
BESCHA_DOMAIN = "https://www.bescha.bund.de"
# Спробуємо RSS URLs по порядку
BESCHA_RSS_CANDIDATES = [
    "https://www.bescha.bund.de/Shared/RSS/Vergabebekanntmachungen",
    "https://www.bescha.bund.de/service/rss-feeds",
    "https://www.bescha.bund.de/rss",
]
BESCHA_HTML_URL = "https://www.bescha.bund.de/DE/Vergaben/Ausschreibungen/ausschreibungen_node.html"

# ── deutsches-ausschreibungsblatt.de (DE, знайдено через DevTools 2026-04-21) ──
DAB_SEARCH_URL = (
    "https://www.deutsches-ausschreibungsblatt.de"
    "/lookup/finder/getResultsBySearchObject"
)
DAB_BASE_URL = "https://www.deutsches-ausschreibungsblatt.de"
# Примітка: "solarcarport" (1 слово) → 1 результат; "solar carport" → 169
# Всі результати повертаються одним запитом без пагінації
DAB_SEARCH_TERMS = [
    "solar carport",
    "photovoltaik carport",
    "parkplatz photovoltaik",
    "parkhaus solar",
    "parkplatzüberdachung",
    "carport pv-anlage",                  # Carport-PV-Anlage variant
    "photovoltaik parkplatzüberdachung",  # Photovoltaik-Parkplatzüberdachung variant
    "parkplatzüberdachung pv-anlage",     # explicit combo
]
# vergabetyp: 1=Ausschreibung (активний), 2=Vergabe (в процесі), 3=Vergebener Auftrag (виданий)
DAB_VERGABETYP_LABELS = {1: "Ausschreibung", 2: "Vergabe", 3: "Vergebener Auftrag"}

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
    elif any(c[:4] in ["0933", "4526", "4422", "4523", "4521", "4522", "4531"] for c in cpv_codes):
        score += 25

    # Keyword-match (0–30)
    kw_count = len(keywords_found)
    if kw_count >= 3:
        score += 30
    elif kw_count == 2:
        score += 20
    elif kw_count == 1:
        score += 10

    # Бюджет (0–20): більший бюджет = вищий score
    if value_eur:
        if value_eur >= 500_000:
            score += 20
        elif value_eur >= 50_000:
            score += 15
        else:
            score += 5
    else:
        score += 5  # невідомий — мінімум

    # Дедлайн (0–10): 10–45д = оптимальне вікно для відповіді
    if days_left is not None:
        if 10 <= days_left <= 45:
            score += 10
        elif days_left < 10:
            score += 2   # занадто терміново
        else:
            score += 5   # >45д — ще є час

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
        "Carport-PV-Anlage",                   # hyphenated compound
        "Photovoltaik-Parkplatzüberdachung",   # hyphenated compound
        "Parkplatzüberdachung mit PV-Anlage",  # explicit phrase
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

    # TED повертає всі типи оголошень (CN, PIN, SCN) за замовчуванням — фільтр notice-type не підтримується в DSL
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


def build_cpv_query(countries: list[str], days: int) -> str:
    """
    Другий TED запит — по специфічних solar CPV кодах.
    Знаходить тендери де 'solar' немає в назві, але CPV вказує на сонячну енергетику.
    Тільки high-confidence solar CPVs (не 45213312 — занадто широкий).
    """
    solar_cpvs = ["09331200", "09332000", "45261215"]  # PV modules, solar install, solar roof
    cpv_parts = " OR ".join(f"classification-cpv IN ({cpv})" for cpv in solar_cpvs)
    country_parts = " OR ".join(f"buyer-country IN ({c})" for c in countries)
    return f"({cpv_parts}) AND ({country_parts}) AND publication-date >= today(-{days})"


def search_ted(countries: list[str], days: int, verbose: bool = True, *, query: str | None = None):
    """
    Генератор: пагінує TED API (page-based) і повертає notices по одному.
    Якщо query передано явно — використовує його (для CPV-based пошуку).
    """
    if query is None:
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


def oeffentlich_notice_url(notice_id: str) -> str:
    """URL на сторінку оголошення oeffentlichevergabe.de."""
    return f"https://oeffentlichevergabe.de/ui/de/notices/{notice_id}"


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

    # Бюджет
    value_eur = extract_value(notice)

    # Score
    # TED query вже відфільтрував за релевантністю (FT + title match).
    # Якщо немає keywords у заголовку → FT-only match (keyword у тілі документа).
    # Базовий бонус 15 гарантує що FT-ліди проходять min-score=20.
    # Примітка: cpv_codes може бути непорожнім з нерелевантними кодами — не враховуємо.
    ft_base = 15 if not keywords_found else 0
    priority_score = ft_base + score_tender(cpv_codes, keywords_found, value_eur, days_left)

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
    Generator: пошук тендерів на simap.ch через REST JSON API.
    Endpoint: GET /rest/publications/v2/project/project-search
    Пагінація через cursor (pagination.lastItem → параметр after).
    """
    if verbose:
        print(f"\n[simap.ch] Пошук ({days}д, {len(SIMAP_SEARCH_TERMS)} terms)...")

    cutoff: date = date.today() - timedelta(days=days)
    seen_ids: set[str] = set()
    yielded = 0

    for term in SIMAP_SEARCH_TERMS:
        params_base = [
            ("search", term),
            ("lang", "de"),
            ("lang", "fr"),
            ("lang", "it"),
            ("lang", "en"),
            ("orderAddressCountryOnlySwitzerland", "true"),
        ]

        after: str | None = None

        while True:
            params = params_base.copy()
            if after:
                params.append(("after", after))

            url = f"{SIMAP_API_URL}?{urllib.parse.urlencode(params)}"
            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "WAT-tender-scraper/1.0 (grandma.agency)",
                    "Referer": "https://www.simap.ch/",
                },
            )

            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read())
            except Exception as e:
                if verbose:
                    print(f"  [{term}] помилка: {e}")
                break

            projects = data.get("projects", [])
            if not projects:
                break

            for proj in projects:
                proj_id = proj.get("id", "")
                if not proj_id or proj_id in seen_ids:
                    continue
                seen_ids.add(proj_id)

                # Фільтр за датою
                pub_date_str = proj.get("publicationDate", "")
                pub_dt: date | None = None
                try:
                    pub_dt = datetime.strptime(pub_date_str[:10], "%Y-%m-%d").date()
                    if pub_dt < cutoff:
                        continue
                except (ValueError, TypeError):
                    pass

                title_dict = proj.get("title", {}) or {}
                title = (
                    title_dict.get("de") or title_dict.get("fr") or
                    title_dict.get("it") or title_dict.get("en") or ""
                )

                proc_office = proj.get("procOfficeName", {}) or {}
                buyer_name = (
                    proc_office.get("de") or proc_office.get("fr") or
                    proc_office.get("it") or ""
                )

                address = proj.get("orderAddress", {}) or {}
                country_id = address.get("countryId", "CH")
                canton_id  = address.get("cantonId", "")
                city_dict  = address.get("city", {}) or {}
                city       = city_dict.get("de") or city_dict.get("fr") or ""

                pub_number = proj.get("publicationNumber") or proj.get("projectNumber") or ""
                pub_id     = proj.get("publicationId", proj_id)

                # Фільтр за ключовими словами в заголовку
                search_text = title.lower()
                has_carport = any(kw.lower() in search_text for kw in CARPORT_KEYWORDS)
                has_parking = any(t.lower()  in search_text for t  in PARKING_PV_TERMS)
                has_solar   = any(t.lower()  in search_text for t  in SOLAR_TERMS)

                if not (has_carport or (has_parking and has_solar)):
                    continue

                keywords_found = [kw for kw in SOLAR_KEYWORDS if kw.lower() in search_text]

                link = (
                    f"https://www.simap.ch/simap/home/publications/tenders.html"
                    f"?publicationId={pub_id}"
                )
                if city and canton_id:
                    buyer_name = f"{buyer_name} ({city}, {canton_id})".strip(" (,)")

                yielded += 1
                yield {
                    "title":          title[:300],
                    "link":           link,
                    "description":    "",
                    "guid":           proj_id,
                    "pub_dt":         pub_dt or date.today(),
                    "buyer_name":     buyer_name,
                    "pub_number":     pub_number,
                    "country_id":     country_id,
                    "keywords_found": keywords_found,
                }

            last_item = data.get("pagination", {}).get("lastItem", "")
            if not last_item or last_item == after or len(projects) == 0:
                break
            after = last_item
            time.sleep(0.5)

        time.sleep(0.5)

    if verbose:
        print(f"  simap.ch: {yielded} релевантних тендерів")


def process_simap_item(item: dict, seen_ids: set, state: dict,
                       min_score: int) -> dict | None:
    """Нормалізує simap.ch REST API project у стандартний tender record."""
    guid      = item["guid"]
    tender_id = f"simap_{guid.replace('-', '_')}" if guid else f"simap_{abs(hash(guid)) % 10**10}"

    if tender_id in state.get("tenders", {}) or tender_id in seen_ids:
        return None

    # REST API не повертає дедлайн і бюджет у search endpoint
    score = 25 + score_tender([], item["keywords_found"], None, None)
    score = min(score, 100)
    if score < min_score:
        return None

    pub_number = item.get("pub_number", "") or tender_id

    return {
        "tender_id":           tender_id,
        "publication_number":  pub_number,
        "title":               item["title"][:300],
        "buyer_name":          item.get("buyer_name", ""),
        "buyer_country":       item.get("country_id", "CH"),
        "buyer_email":         "",
        "cpv_codes":           "",
        "keyword_match":       ",".join(item["keywords_found"]),
        "estimated_value_eur": "",
        "deadline_date":       "",
        "days_until_deadline": "",
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


# ── bescha.bund.de (Federal Procurement Office) ────────────────────────────────

def search_bescha_de(days: int, verbose: bool = True):
    """
    Generator: завантажує bescha.bund.de RSS (або HTML) і фільтрує за keywords.
    Джерело: Федеральний офіс закупівель (Beschaffungsamt des BMI).
    """
    if verbose:
        print(f"\n[bescha.bund.de] Завантаження ({days}д)...")

    cutoff = date.today() - timedelta(days=days)
    yielded = 0
    rss_found = False

    # Спробуємо RSS URLs по порядку
    for rss_url in BESCHA_RSS_CANDIDATES:
        try:
            req = urllib.request.Request(
                rss_url,
                headers={"User-Agent": "WAT-tender-scraper/1.0 (grandma.agency)"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
            root = ET.fromstring(raw)
            items = root.findall(".//item")
            if items:
                rss_found = True
                if verbose:
                    print(f"  RSS знайдено: {rss_url}")
                break
        except Exception:
            continue

    if not rss_found:
        if verbose:
            print(f"  RSS недоступна, спроба HTML скрейпингу...")
        return  # Для простоти, HTML скрейпинг можна додати пізніше

    if not items:
        if verbose:
            print(f"  0 items")
        return

    if verbose:
        print(f"  {len(items)} items, фільтруємо...")

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
        has_parking = any(t.lower() in search_text for t in PARKING_PV_TERMS)
        has_solar   = any(t.lower() in search_text for t in SOLAR_TERMS)

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
        print(f"  bescha.bund.de: {yielded} релевантних items")


def process_bescha_item(item: dict, seen_ids: set, state: dict,
                        min_score: int) -> dict | None:
    """Нормалізує bescha.bund.de RSS item у стандартний tender record."""
    slug      = item["guid"].rstrip("/").split("/")[-1].replace(".html", "")
    tender_id = f"bescha_{slug}" if slug else f"bescha_{abs(hash(item['guid'])) % 10**10}"

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
        "source":              "bescha.bund.de",
    }


# ── deutsches-ausschreibungsblatt.de (DE) ─────────────────────────────────────

class _PostRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Слідує 307/308 редіректам зберігаючи POST метод і тіло."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if code in (307, 308):
            return urllib.request.Request(
                newurl,
                data=req.data,
                headers={k: v for k, v in req.header_items()
                         if k.lower() not in ("host", "content-length")},
                method="POST",
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_DAB_OPENER = urllib.request.build_opener(_PostRedirectHandler())


def search_dab_de(days: int, verbose: bool = True):
    """
    Generator: пошук тендерів на deutsches-ausschreibungsblatt.de через POST API.
    Всі результати повертаються одним запитом (без пагінації).
    Безкоштовно: назва, місто, дедлайн, тип. Повний текст — за підпискою.
    """
    if verbose:
        print(f"\n[DAB.de] Пошук ({days}д, {len(DAB_SEARCH_TERMS)} terms)...")

    today = date.today()
    seen_ids: set[str] = set()
    yielded = 0

    headers = {
        "Content-Type": "application/json",
        "Referer": f"{DAB_BASE_URL}/auftrag-finden",
        "Origin": DAB_BASE_URL,
        "User-Agent": "WAT-tender-scraper/1.0 (grandma.agency)",
    }

    for term in DAB_SEARCH_TERMS:
        body = json.dumps({
            "searchData": {"suchbegriffe": {"begriffe": term}}
        }).encode("utf-8")
        req = urllib.request.Request(
            DAB_SEARCH_URL, data=body, headers=headers, method="POST"
        )

        try:
            with _DAB_OPENER.open(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            if verbose:
                print(f"  [{term}] помилка: {e}")
            continue

        tenders = data.get("payload", {}).get("kopfdaten", [])
        if verbose:
            print(f"  [{term}] {len(tenders)} результатів")

        for t in tenders:
            uuid = t.get("uuid", "")
            if not uuid or uuid in seen_ids:
                continue
            seen_ids.add(uuid)

            # Дедлайн/закінчення оголошення
            anzeige_ende = t.get("anzeige_ende", "")
            deadline_dt: date | None = None
            try:
                deadline_dt = datetime.strptime(anzeige_ende[:10], "%Y-%m-%d").date()
                # Пропускаємо вже прострочені оголошення
                if deadline_dt < today:
                    continue
            except (ValueError, TypeError):
                pass

            # Фільтр за тематикою (в назві)
            title = t.get("titel", "") or ""
            search_text = title.lower()
            # Явні solar-carport терміни не потребують AND Solar
            has_explicit = any(
                kw.lower() in search_text
                for kw in ["solarcarport", "solar-carport", "pv-carport", "parkplatzüberdachung"]
            )
            # Загальний "carport" — тільки якщо є Solar/PV поряд
            has_generic_carport = "carport" in search_text
            has_parking = any(pt.lower() in search_text for pt in PARKING_PV_TERMS)
            has_solar   = any(st.lower() in search_text for st in SOLAR_TERMS)

            if not (has_explicit or
                    (has_generic_carport and has_solar) or
                    (has_parking and has_solar)):
                continue

            keywords_found = [kw for kw in SOLAR_KEYWORDS if kw.lower() in search_text]
            vergabetyp = t.get("vergabetyp", 0)
            ort = t.get("ort", "")

            # Формуємо slug для URL (як на сайті)
            slug = re.sub(r"[^a-z0-9\-]", "", re.sub(r"\s+", "-",
                title.lower()
                .replace(":", "").replace("/", "")
                .replace("ä", "ae").replace("ö", "oe")
                .replace("ü", "ue").replace("ß", "ss")
            )).strip("-")
            link = f"{DAB_BASE_URL}/ausschreibung#/{slug}_{uuid}"

            days_left = (deadline_dt - today).days if deadline_dt else None

            yielded += 1
            yield {
                "uuid":           uuid,
                "title":          title[:300],
                "ort":            ort,
                "link":           link,
                "anzeige_ende":   anzeige_ende,
                "deadline_dt":    deadline_dt,
                "days_left":      days_left,
                "vergabetyp":     vergabetyp,
                "eu_vergabe":     t.get("eu_vergabe", 0),
                "keywords_found": keywords_found,
            }

        time.sleep(0.5)

    if verbose:
        print(f"  DAB.de: {yielded} релевантних тендерів")


def process_dab_item(item: dict, seen_ids: set, state: dict,
                     min_score: int) -> dict | None:
    """Нормалізує DAB.de тендер у стандартний tender record."""
    tender_id = f"dab_{item['uuid'].replace('-', '_')}"

    if tender_id in state.get("tenders", {}) or tender_id in seen_ids:
        return None

    days_left  = item.get("days_left")
    deadline_dt = item.get("deadline_dt")

    # Score: тільки keywords і дедлайн (CPV і бюджет за підпискою)
    # Базовий 25 — DAB вже пошукав за нашими термінами
    # Ausschreibung (vergabetyp=1) отримує додатковий бонус +10
    vergabetyp_bonus = 10 if item.get("vergabetyp") == 1 else 0
    score = 25 + vergabetyp_bonus + score_tender([], item["keywords_found"], None, days_left)
    score = min(score, 100)
    if score < min_score:
        return None

    vergabetyp_label = DAB_VERGABETYP_LABELS.get(item.get("vergabetyp", 0), "")
    buyer_name = f"{item['ort']} ({vergabetyp_label})" if vergabetyp_label else item["ort"]

    return {
        "tender_id":           tender_id,
        "publication_number":  item["uuid"],
        "title":               item["title"][:300],
        "buyer_name":          buyer_name,
        "buyer_country":       "DE",
        "buyer_email":         "",
        "cpv_codes":           "",
        "keyword_match":       ",".join(item["keywords_found"]),
        "estimated_value_eur": "",
        "deadline_date":       item["anzeige_ende"],
        "days_until_deadline": days_left if days_left is not None else "",
        "publication_date":    "",
        "priority_score":      score,
        "ted_url":             item["link"],
        "source":              "DAB.de",
    }


# ── oeffentlichevergabe.de (Bekanntmachungsservice) ───────────────────────────

def search_oeffentlich_de(countries: list[str], days: int, verbose: bool = True):
    """
    Generator: завантажує денні ZIP з oeffentlichevergabe.de OpenData API,
    фільтрує за keywords і країнами DACH.

    API: GET /api/notice-exports?pubDay=YYYY-MM-DD&format=csv.zip
    ZIP містить 15+ CSV (нормалізована структура eForms):
      notice.csv         — основні поля (noticeIdentifier, noticeType, publicationDate)
      purpose.csv        — заголовок, опис, бюджет на lot
      organisation.csv   — buyer (organisationRole = "buyer")
      classification.csv — CPV-коди

    Обмеження: максимум OEFFENTLICH_MAX_DAYS днів (30) щоб не перевантажувати API.
    """
    import zipfile
    import io as _io
    import csv as csv_mod

    if verbose:
        print(f"\n[oeffentlichevergabe.de] Завантаження ({min(days, OEFFENTLICH_MAX_DAYS)}д)...")

    today = date.today()
    effective_days = min(days, OEFFENTLICH_MAX_DAYS)
    yielded = 0

    for d in range(1, effective_days + 1):
        target_day = (today - timedelta(days=d)).strftime("%Y-%m-%d")
        url = f"{OEFFENTLICH_API_URL}?pubDay={target_day}&format=csv.zip"

        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "WAT-tender-scraper/1.0 (grandma.agency)",
                    "Accept": "*/*",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                zip_data = resp.read()
        except Exception as e:
            if verbose and d == 1:
                print(f"  [{target_day}] помилка: {e}")
            continue

        try:
            zf = zipfile.ZipFile(_io.BytesIO(zip_data))
        except Exception as e:
            if verbose:
                print(f"  [{target_day}] ZIP error: {e}")
            continue

        def _read_csv(name: str) -> list[dict]:
            if name not in zf.namelist():
                return []
            with zf.open(name) as f:
                content = f.read().decode("utf-8", errors="ignore")
            return list(csv_mod.DictReader(_io.StringIO(content)))

        # Завантажуємо потрібні таблиці
        notice_map = {r["noticeIdentifier"]: r for r in _read_csv("notice.csv")}
        classification_map = {r["noticeIdentifier"]: r for r in _read_csv("classification.csv")}

        # Buyer lookup — беремо рядки де role = "buyer"
        buyer_map: dict[str, dict] = {}
        for org in _read_csv("organisation.csv"):
            if org.get("organisationRole") == "buyer":
                buyer_map[org["noticeIdentifier"]] = org

        # Фільтруємо за країнами
        country_notice_ids = {
            nid for nid, org in buyer_map.items()
            if org.get("organisationCountryCode", "") in countries
        }

        day_yielded = 0
        for purpose in _read_csv("purpose.csv"):
            nid = purpose.get("noticeIdentifier", "")
            if not nid:
                continue
            # Фільтр за країною
            if nid not in country_notice_ids:
                continue

            title = purpose.get("title", "")
            description = purpose.get("description", "")
            search_text = (title + " " + description).lower()

            has_carport = any(kw.lower() in search_text for kw in CARPORT_KEYWORDS)
            has_parking = any(t.lower() in search_text for t in PARKING_PV_TERMS)
            has_solar = any(t.lower() in search_text for t in SOLAR_TERMS)

            if not (has_carport or (has_parking and has_solar)):
                continue

            keywords_found = [kw for kw in SOLAR_KEYWORDS if kw.lower() in search_text]

            org = buyer_map.get(nid, {})
            cpv = classification_map.get(nid, {})
            notice = notice_map.get(nid, {})

            value_raw = purpose.get("estimatedValue", "")
            try:
                value_eur: float | None = float(value_raw) if value_raw else None
            except (ValueError, TypeError):
                value_eur = None

            lot_id = purpose.get("lotIdentifier", "LOT-0000")
            pub_date = notice.get("publicationDate", "")[:10]

            day_yielded += 1
            yielded += 1
            yield {
                "notice_id":      nid,
                "lot_id":         lot_id,
                "title":          title[:300],
                "pub_date":       pub_date,
                "notice_type":    notice.get("noticeType", ""),
                "buyer_name":     org.get("organisationName", ""),
                "buyer_country":  org.get("organisationCountryCode", ""),
                "buyer_city":     org.get("organisationCity", ""),
                "cpv_code":       cpv.get("mainClassificationCode", ""),
                "value_eur":      value_eur,
                "keywords_found": keywords_found,
            }

        if verbose:
            print(f"  [{target_day}] {day_yielded} matches")

        time.sleep(0.5)

    if verbose:
        print(f"  oeffentlichevergabe.de: {yielded} релевантних тендерів")


def process_oeffentlich_item(item: dict, seen_ids: set, state: dict,
                              min_score: int) -> dict | None:
    """Нормалізує oeffentlichevergabe.de запис у стандартний tender record."""
    nid = item["notice_id"]
    lot_id = item.get("lot_id", "LOT-0000")
    tender_id = f"oev_{nid.replace('-', '_')}_{lot_id.replace('-', '_')}"

    if tender_id in state.get("tenders", {}) or tender_id in seen_ids:
        return None

    cpv_code = item.get("cpv_code", "")
    cpv_codes = [cpv_code] if cpv_code else []

    score = score_tender(cpv_codes, item["keywords_found"], item.get("value_eur"), None)
    if score < min_score:
        return None

    country_raw = item.get("buyer_country", "")
    country_a2 = COUNTRY_ALPHA2.get(country_raw, country_raw[:2] if len(country_raw) >= 2 else "DE")

    buyer = item.get("buyer_name", "")
    city = item.get("buyer_city", "")
    if city and city not in buyer:
        buyer = f"{buyer} ({city})"

    return {
        "tender_id":           tender_id,
        "publication_number":  nid,
        "title":               item["title"],
        "buyer_name":          buyer,
        "buyer_country":       country_a2,
        "buyer_email":         "",
        "cpv_codes":           cpv_code,
        "keyword_match":       ",".join(item["keywords_found"]),
        "estimated_value_eur": item.get("value_eur") or "",
        "deadline_date":       "",
        "days_until_deadline": "",
        "publication_date":    item.get("pub_date", ""),
        "priority_score":      score,
        "ted_url":             oeffentlich_notice_url(nid),
        "source":              "oeffentlichevergabe.de",
    }


# ── open.nrw (NRW CKAN API) ────────────────────────────────────────────────────

def search_open_nrw(days: int, verbose: bool = True):
    """
    Generator: завантажує CKAN dataset з open.nrw і фільтрує за keywords.
    Джерело: Nordrhein-Westfalen (NRW) OpenData portal.
    """
    if verbose:
        print(f"\n[open.nrw CKAN] Завантаження ({days}д)...")

    try:
        url = f"{OPEN_NRW_CKAN_URL}?id={OPEN_NRW_PACKAGE_ID}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "WAT-tender-scraper/1.0 (grandma.agency)"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            package_data = json.loads(resp.read())
    except Exception as e:
        if verbose:
            print(f"  CKAN API error: {e}")
        return

    # Знаходимо найсвіжіший CSV/JSON ресурс
    package = package_data.get("result", {})
    resources = package.get("resources", [])

    if not resources:
        if verbose:
            print(f"  Нема ресурсів у CKAN пакеті")
        return

    # Шукаємо CSV ресурс
    csv_url = None
    for res in resources:
        name = res.get("name", "").lower()
        url_res = res.get("url", "")
        if "csv" in name or url_res.endswith(".csv"):
            csv_url = url_res
            break

    if not csv_url:
        if verbose:
            print(f"  CSV ресурс не знайдено")
        return

    if verbose:
        print(f"  Завантаження CSV: {csv_url}")

    try:
        import io as _io
        import csv as csv_mod

        req = urllib.request.Request(
            csv_url,
            headers={"User-Agent": "WAT-tender-scraper/1.0 (grandma.agency)"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            csv_content = resp.read().decode("utf-8", errors="ignore")

        cutoff = date.today() - timedelta(days=days)
        yielded = 0

        reader = csv_mod.DictReader(_io.StringIO(csv_content))
        for row in reader:
            # Спробуємо розпізнати дату в різних форматах
            pub_date_raw = row.get("Veröffentlichungsdatum", "") or row.get("publication_date", "")
            pub_dt: date | None = None
            try:
                pub_dt = datetime.strptime(pub_date_raw[:10], "%Y-%m-%d").date()
                if pub_dt < cutoff:
                    continue
            except (ValueError, TypeError):
                pass

            title = row.get("Titel", "") or row.get("title", "")
            description = row.get("Beschreibung", "") or row.get("description", "")
            search_text = (title + " " + description).lower()

            has_carport = any(kw.lower() in search_text for kw in CARPORT_KEYWORDS)
            has_parking = any(t.lower() in search_text for t in PARKING_PV_TERMS)
            has_solar   = any(t.lower() in search_text for t in SOLAR_TERMS)

            if not (has_carport or (has_parking and has_solar)):
                continue

            keywords_found = [kw for kw in SOLAR_KEYWORDS if kw.lower() in search_text]
            link = row.get("Link", "") or ""

            yielded += 1
            yield {
                "title":          title[:300],
                "description":    description[:300],
                "link":           link,
                "pub_dt":         pub_dt or date.today(),
                "guid":           link or f"nrw_open_{abs(hash(title)) % 10**10}",
                "keywords_found": keywords_found,
            }

        if verbose:
            print(f"  open.nrw: {yielded} релевантних записів")

    except Exception as e:
        if verbose:
            print(f"  CSV parsing error: {e}")


def process_open_nrw_item(item: dict, seen_ids: set, state: dict,
                          min_score: int) -> dict | None:
    """Нормалізує open.nrw CSV запис у стандартний tender record."""
    guid = item["guid"]
    tender_id = f"nrw_open_{guid.replace('-', '_').replace('/', '_')[:50]}"

    if tender_id in state.get("tenders", {}) or tender_id in seen_ids:
        return None

    score = 25 + score_tender([], item["keywords_found"], None, None)
    score = min(score, 100)
    if score < min_score:
        return None

    return {
        "tender_id":           tender_id,
        "publication_number":  guid[:100] or tender_id,
        "title":               item["title"],
        "buyer_name":          "",
        "buyer_country":       "DE",
        "buyer_email":         "",
        "cpv_codes":           "",
        "keyword_match":       ",".join(item["keywords_found"]),
        "estimated_value_eur": "",
        "deadline_date":       "",
        "days_until_deadline": "",
        "publication_date":    item["pub_dt"].isoformat(),
        "priority_score":      score,
        "ted_url":             item["link"],
        "source":              "open.nrw",
    }


# ── e-vergabe-sh.de (Schleswig-Holstein) ─────────────────────────────────────

EVERGABE_SH_BASE = "https://www.e-vergabe-sh.de/vergabeplattform/bekanntmachungen"
EVERGABE_SH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WAT-tender-scraper/1.0; +https://grandma.agency)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9",
}


def _sh_get_html(url: str, timeout: int = 20) -> str | None:
    """Завантажує HTML сторінку S-H порталу."""
    try:
        req = urllib.request.Request(url, headers=EVERGABE_SH_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
        charset = "utf-8"
        ct = r.headers.get("Content-Type", "")
        if "charset=" in ct:
            charset = ct.split("charset=")[-1].strip().split(";")[0]
        return raw.decode(charset, errors="replace")
    except Exception:
        return None


def _sh_extract_items(html: str) -> list[dict]:
    """Парсить bek_list_item блоки зі S-H HTML (TYPO3 CMS)."""
    items: list[dict] = []

    # Розбиваємо на блоки по початку кожного bek_list_item
    chunks = re.split(r'(?=<div[^>]+class="[^"]*bek_list_item[^"]*"[^>]+data-item-id=")', html)

    for chunk in chunks:
        id_m = re.search(r'data-item-id="(\d+)"', chunk)
        if not id_m:
            continue
        item_id = id_m.group(1)

        # Title
        title_m = re.search(
            r'class="bek_list_item_headline"[^>]*>\s*(.*?)\s*</div>',
            chunk, re.DOTALL,
        )
        title = _strip_html(title_m.group(1)).strip() if title_m else ""

        # Info (procedure + location)
        info_m = re.search(
            r'class="bek_list_item_info"[^>]*>\s*(.*?)\s*</div>',
            chunk, re.DOTALL,
        )
        info_text = re.sub(r'\s+', ' ', _strip_html(info_m.group(1))).strip() if info_m else ""

        # Info2 (dates)
        info2_m = re.search(
            r'class="bek_list_item_info2"[^>]*>\s*(.*?)\s*</div>',
            chunk, re.DOTALL,
        )
        info2_text = re.sub(r'\s+', ' ', _strip_html(info2_m.group(1))).strip() if info2_m else ""

        # Deadline "Frist: DD.MM.YYYY"
        frist_m = re.search(r'Frist\s*:\s*(\d{1,2}\.\d{1,2}\.\d{4})', info2_text)
        deadline_raw = frist_m.group(1) if frist_m else ""

        # Publication date "Online Seit: DD.MM.YYYY"
        online_m = re.search(r'Online\s+Seit\s*:\s*(\d{1,2}\.\d{1,2}\.\d{4})', info2_text, re.IGNORECASE)
        pub_raw = online_m.group(1) if online_m else ""

        items.append({
            "item_id":      item_id,
            "title":        title[:300],
            "info":         info_text,
            "deadline_raw": deadline_raw,
            "pub_raw":      pub_raw,
        })

    return items


def _sh_parse_date(dmy: str) -> date | None:
    """DD.MM.YYYY → date."""
    try:
        d, mo, y = dmy.strip().split(".")
        return date(int(y), int(mo), int(d))
    except Exception:
        return None


def search_evergabe_sh(days: int, verbose: bool = True):
    """
    Generator: скрейпить e-vergabe-sh.de (Schleswig-Holstein) і фільтрує за keywords.
    Портал — статичний TYPO3 HTML, пагінація через ?page=N.
    """
    if verbose:
        print(f"\n[e-vergabe-sh.de] Завантаження ({days}д)...")

    cutoff = date.today() - timedelta(days=days)
    yielded = 0
    page = 1

    while True:
        url = EVERGABE_SH_BASE if page == 1 else f"{EVERGABE_SH_BASE}?page={page}"
        html = _sh_get_html(url)
        if not html:
            if verbose:
                print(f"  S-H: помилка завантаження стор.{page}")
            break

        items = _sh_extract_items(html)
        if not items:
            break  # кінець пагінації

        found_on_page = False
        for item in items:
            pub_dt = _sh_parse_date(item["pub_raw"])
            if pub_dt and pub_dt < cutoff:
                continue

            search_text = (item["title"] + " " + item["info"]).lower()
            has_carport = any(kw.lower() in search_text for kw in CARPORT_KEYWORDS)
            has_parking = any(t.lower()  in search_text for t  in PARKING_PV_TERMS)
            has_solar   = any(t.lower()  in search_text for t  in SOLAR_TERMS)

            if not (has_carport or (has_parking and has_solar)):
                continue

            keywords_found = [kw for kw in SOLAR_KEYWORDS if kw.lower() in search_text]
            found_on_page = True
            yielded += 1
            item["pub_dt"]       = pub_dt or date.today()
            item["keywords_found"] = keywords_found
            yield item

        # Якщо всі items старіші за cutoff — зупиняємось (portal sorted newest first)
        if not found_on_page:
            oldest = None
            for item in items:
                d = _sh_parse_date(item["pub_raw"])
                if d:
                    oldest = d
            if oldest and oldest < cutoff:
                break

        page += 1
        if page > 50:  # safety cap
            break

    if verbose:
        print(f"  e-vergabe-sh.de: {yielded} релевантних items")


def process_evergabe_sh_item(item: dict, seen_ids: set, state: dict,
                              min_score: int) -> dict | None:
    """Нормалізує S-H item у стандартний tender record."""
    tender_id = f"sh_{item['item_id']}"

    if tender_id in state.get("tenders", {}) or tender_id in seen_ids:
        return None

    deadline_dt  = _sh_parse_date(item["deadline_raw"])
    deadline_iso = deadline_dt.isoformat() if deadline_dt else ""
    days_left: int | None = None
    if deadline_dt:
        days_left = (deadline_dt - date.today()).days

    score = 20 + score_tender([], item["keywords_found"], None, days_left)
    score = min(score, 100)
    if score < min_score:
        return None

    link = f"{EVERGABE_SH_BASE}/{item['item_id']}"

    return {
        "tender_id":           tender_id,
        "publication_number":  item["item_id"],
        "title":               item["title"],
        "buyer_name":          item["info"][:200],
        "buyer_country":       "DE",
        "buyer_email":         "",
        "cpv_codes":           "",
        "keyword_match":       ",".join(item["keywords_found"]),
        "estimated_value_eur": "",
        "deadline_date":       deadline_iso,
        "days_until_deadline": days_left if days_left is not None else "",
        "publication_date":    item["pub_dt"].isoformat(),
        "priority_score":      score,
        "ted_url":             link,
        "source":              "e-vergabe-sh",
    }


# ── Health Check ──────────────────────────────────────────────────────────────

def run_health_check(sources: list[str] | None = None) -> None:
    """
    Перевіряє доступність усіх джерел.
    Виводить таблицю статусу. Exit code 1 якщо є помилки.
    """
    if sources is None:
        sources = ["ted", "bund", "simap", "nrw", "dab", "oeffentlich", "sh"]

    results: dict[str, dict] = {}
    for s in sources:
        results[s] = {"name": s, "status": "?", "detail": "?", "time": "?"}

    SOURCE_NAMES = {
        "ted": "TED EU API",
        "bund": "Bund.de RSS",
        "simap": "simap.ch",
        "nrw": "vergabe.nrw",
        "dab": "DAB.de",
        "oeffentlich": "oeffentlichevergabe.de",
        "sh": "e-vergabe-sh.de",
        "bescha": "bescha.bund.de",
        "nrw-open": "Open.NRW CKAN",
    }

    failed = []

    for source_key in sources:
        t0 = time.time()
        try:
            if source_key == "ted":
                payload = build_payload(build_query(["DEU"], days=1), page=1, page_size=1)
                data = fetch_page(payload, verbose=False)
                ok = data is not None and "notices" in data
                detail = f"{len(data.get('notices', []))} notices" if ok else "API error"

            elif source_key == "bund":
                req = urllib.request.Request(BUND_RSS_URL, headers={"User-Agent": "WAT-tender-scraper/1.0"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    raw = r.read()
                root = ET.fromstring(raw)
                items = root.findall(".//item")
                ok = True
                detail = f"{len(items)} items"

            elif source_key == "oeffentlich":
                target_day = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
                url = f"{OEFFENTLICH_API_URL}?pubDay={target_day}&format=csv.zip"
                req = urllib.request.Request(url, headers={"User-Agent": "WAT-tender-scraper/1.0"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    zdata = r.read()
                import zipfile as _zf, io as _io
                zf = _zf.ZipFile(_io.BytesIO(zdata))
                ok = "notice.csv" in zf.namelist()
                detail = f"{len(zf.namelist())} files"

            else:
                # Generic check — HTTP HEAD request (надійніше ніж raw socket)
                portal_urls = {
                    "simap": "https://www.simap.ch",
                    "nrw": "https://www.vergabe.nrw.de",
                    "dab": "https://www.deutsches-ausschreibungsblatt.de",
                    "sh": "https://www.e-vergabe-sh.de/vergabeplattform/bekanntmachungen",
                    "bescha": "https://www.bescha.bund.de",
                    "nrw-open": "https://open.nrw",
                }
                url = portal_urls.get(source_key, "https://example.com")
                req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "WAT-tender-scraper/1.0"})
                try:
                    with urllib.request.urlopen(req, timeout=10) as r:
                        ok = r.status < 500
                        detail = f"HTTP {r.status}"
                except urllib.error.HTTPError as e:
                    ok = e.code < 500
                    detail = f"HTTP {e.code}"
                except Exception as e:
                    ok = False
                    detail = str(e)[:30]

        except Exception as e:
            ok = False
            detail = str(e)[:35]

        elapsed = time.time() - t0
        results[source_key]["name"] = SOURCE_NAMES.get(source_key, source_key)
        results[source_key]["status"] = "✅ OK" if ok else "❌ ERROR"
        results[source_key]["detail"] = detail
        results[source_key]["time"] = f"{elapsed:.1f}s"
        if not ok:
            failed.append(source_key)

    # Print table
    print("\n📋 Tender Scraper — Health Check\n")
    col_w = [28, 12, 30, 8]
    header = ["Source", "Status", "Detail", "Time"]

    print("┌" + "┬".join("─" * w for w in col_w) + "┐")
    print("│" + "│".join(f" {h:<{w-1}}" for h, w in zip(header, col_w)) + "│")
    print("├" + "┼".join("─" * w for w in col_w) + "┤")
    for key in sources:
        r = results[key]
        cols = [r["name"][:26], r["status"][:10], r["detail"][:28], r["time"][:6]]
        print("│" + "│".join(f" {c:<{w-1}}" for c, w in zip(cols, col_w)) + "│")
    print("└" + "┴".join("─" * w for w in col_w) + "┘")

    total = len(sources)
    passed = total - len(failed)
    print(f"\n✓ {passed}/{total} джерела відповідають")
    if failed:
        print(f"❌ Помилка: {', '.join(failed)}")
        sys.exit(1)
    else:
        print("✅ Всі джерела OK")
        sys.exit(0)


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
        choices=["all", "ted", "bund", "simap", "nrw", "dab", "oeffentlich", "sh", "bescha", "nrw-open"],
        help="Джерело: all (TED+Bund.de+simap.ch+vergabe.nrw+DAB.de+oeffentlichevergabe.de+e-vergabe-sh.de), ted, bund, simap, nrw, dab, oeffentlich, sh, bescha, nrw-open (default: all)",
    )
    parser.add_argument(
        "--health-check",
        action="store_true",
        help="Перевірити доступність всіх джерел і вихід",
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
        active_sources.append("TED (keywords + CPV)")
    if args.source in ("all", "bund") and region in ("DE", "DACH"):
        active_sources.append("Bund.de")
    if args.source in ("all", "simap") and region in ("CH", "DACH"):
        active_sources.append("simap.ch")
    if args.source in ("all", "nrw") and region in ("DE", "DACH"):
        active_sources.append("vergabe.nrw")
    if args.source in ("all", "dab") and region in ("DE", "DACH"):
        active_sources.append("DAB.de")
    if args.source in ("all", "oeffentlich") and region in ("DE", "DACH"):
        active_sources.append("oeffentlichevergabe.de")
    if args.source in ("all", "sh") and region in ("DE", "DACH"):
        active_sources.append("e-vergabe-sh.de")

    if args.health_check:
        hc_sources = None if args.source == "all" else [args.source]
        run_health_check(hc_sources)
        # run_health_check calls sys.exit() so we won't reach here

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

    def _clean_record(record: dict) -> dict:
        """Strip newlines from all string fields to prevent CSV parse errors."""
        return {k: re.sub(r'[\r\n]+', ' ', v).strip() if isinstance(v, str) else v
                for k, v in record.items()}

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
            writer.writerow(_clean_record(record))
            csv_file.flush()
            seen_ids.add(record["tender_id"])
            if not args.no_dedup:
                state["tenders"][record["tender_id"]] = record["publication_number"]
            total_records += 1
            if verbose and total_records % 10 == 0:
                print(f"  ✓ {total_records} тендерів записано...")

    # ── TED (CPV-based — доповнює keyword пошук, дедуп автоматичний) ────────────────────
    if args.source in ("all", "ted"):
        cpv_q = build_cpv_query(countries, args.days)
        if verbose:
            print("[TED CPV] Пошук за CPV 09331200/09332000/45261215...")
        for notice in search_ted(countries, args.days, verbose=verbose, query=cpv_q):
            record = process_notice(notice, seen_ids, dedup_state, args.min_score)
            if record is None:
                continue
            ensure_writer(record)
            writer.writerow(_clean_record(record))
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
            writer.writerow(_clean_record(record))
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
            writer.writerow(_clean_record(record))
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
            writer.writerow(_clean_record(record))
            csv_file.flush()
            seen_ids.add(record["tender_id"])
            if not args.no_dedup:
                state["tenders"][record["tender_id"]] = record["publication_number"]
            total_records += 1
            if verbose and total_records % 10 == 0:
                print(f"  ✓ {total_records} тендерів записано...")

    # ── DAB.de (тільки для DE або DACH) ──────────────────────────────────────────
    if args.source in ("all", "dab") and region in ("DE", "DACH"):
        for item in search_dab_de(args.days, verbose=verbose):
            record = process_dab_item(item, seen_ids, dedup_state, args.min_score)
            if record is None:
                continue
            ensure_writer(record)
            writer.writerow(_clean_record(record))
            csv_file.flush()
            seen_ids.add(record["tender_id"])
            if not args.no_dedup:
                state["tenders"][record["tender_id"]] = record["publication_number"]
            total_records += 1
            if verbose and total_records % 10 == 0:
                print(f"  ✓ {total_records} тендерів записано...")

    # ── oeffentlichevergabe.de (тільки для DE або DACH) ─────────────────────────────────────────
    if args.source in ("all", "oeffentlich") and region in ("DE", "DACH"):
        for item in search_oeffentlich_de(countries, args.days, verbose=verbose):
            record = process_oeffentlich_item(item, seen_ids, dedup_state, args.min_score)
            if record is None:
                continue
            ensure_writer(record)
            writer.writerow(_clean_record(record))
            csv_file.flush()
            seen_ids.add(record["tender_id"])
            if not args.no_dedup:
                state["tenders"][record["tender_id"]] = record["publication_number"]
            total_records += 1
            if verbose and total_records % 10 == 0:
                print(f"  ✓ {total_records} тендерів записано...")

    # ── bescha.bund.de (тільки явний --source bescha; з "all" виключено — evergabe-online.de повертає HTTP 400) ──
    if args.source == "bescha" and region in ("DE", "DACH"):
        for item in search_bescha_de(args.days, verbose=verbose):
            record = process_bescha_item(item, seen_ids, dedup_state, args.min_score)
            if record is None:
                continue
            ensure_writer(record)
            writer.writerow(_clean_record(record))
            csv_file.flush()
            seen_ids.add(record["tender_id"])
            if not args.no_dedup:
                state["tenders"][record["tender_id"]] = record["publication_number"]
            total_records += 1
            if verbose and total_records % 10 == 0:
                print(f"  ✓ {total_records} тендерів записано...")

    # ── e-vergabe-sh.de (Schleswig-Holstein) ─────────────────────────────────────
    if args.source in ("all", "sh") and region in ("DE", "DACH"):
        for item in search_evergabe_sh(args.days, verbose=verbose):
            record = process_evergabe_sh_item(item, seen_ids, dedup_state, args.min_score)
            if record is None:
                continue
            ensure_writer(record)
            writer.writerow(_clean_record(record))
            csv_file.flush()
            seen_ids.add(record["tender_id"])
            if not args.no_dedup:
                state["tenders"][record["tender_id"]] = record["publication_number"]
            total_records += 1
            if verbose and total_records % 10 == 0:
                print(f"  ✓ {total_records} тендерів записано...")

    # ── open.nrw (тільки явний --source nrw-open; з "all" виключено — CKAN XML endpoints повертають 404) ──
    if args.source == "nrw-open" and region in ("DE", "DACH"):
        for item in search_open_nrw(args.days, verbose=verbose):
            record = process_open_nrw_item(item, seen_ids, dedup_state, args.min_score)
            if record is None:
                continue
            ensure_writer(record)
            writer.writerow(_clean_record(record))
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
