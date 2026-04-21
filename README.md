# tender-scraper

Пошук активних тендерів на solar carport / PV через TED API (Tenders Electronic Daily, ЄС).

**Логіка:** компанії та муніципалітети, які вже виділили бюджет = гарячі ліди.  
**Регіон:** DACH (Німеччина, Австрія, Швейцарія) + вся ЄС.  
**Джерело:** [ted.europa.eu](https://ted.europa.eu) — офіційна ЄС база публічних закупівель, безкоштовний API.

---

## Швидкий старт

```bash
# Пошук за останні 7 днів (DACH, score ≥ 30)
python tender_scraper.py

# Тільки Німеччина, за 30 днів
python tender_scraper.py --region DE --days 30

# Підвищений поріг якості
python tender_scraper.py --min-score 50

# Перевірити запит без виконання
python tender_scraper.py --dry-run

# Тижневий дайджест → Telegram
python tender_digest.py
```

## Скрипти

### `tender_scraper.py` — головний скрапер

| Аргумент | Default | Опис |
|----------|---------|------|
| `--region` | `DACH` | `DE`, `AT`, `CH` або `DACH` |
| `--days` | `7` | Кількість днів назад |
| `--min-score` | `30` | Мінімальний пріоритет 0–100 |
| `--output` | авто | Шлях до CSV |
| `--quiet` | — | Без verbose виводу |
| `--no-telegram` | — | Без Telegram |
| `--dry-run` | — | Показати запит, не виконувати |

### `tender_digest.py` — тижневий дайджест

| Аргумент | Default | Опис |
|----------|---------|------|
| `--region` | `DACH` | Регіон |
| `--min-score` | `50` | Поріг для дайджесту |
| `--days` | `7` | Діапазон пошуку |
| `--top` | `10` | Кількість тендерів у Telegram |

## CSV-колонки

| Колонка | Опис |
|---------|------|
| `tender_id` | Унікальний ID (dedup ключ) |
| `notice_number` | Номер тендера TED (напр. `2026/S 123-456789`) |
| `title` | Назва тендера |
| `buyer_name` | Назва замовника |
| `buyer_country` | Країна (DE/AT/CH) |
| `buyer_email` | Email для контакту |
| `buyer_phone` | Телефон |
| `description` | Перші 500 символів опису |
| `cpv_codes` | CPV коди через кому |
| `keyword_match` | Знайдені ключові слова |
| `estimated_value_eur` | Бюджет в EUR |
| `deadline_date` | Дедлайн подачі заявки |
| `days_until_deadline` | Днів до дедлайну |
| `publication_date` | Дата публікації |
| `priority_score` | Пріоритет 0–100 |
| `ted_url` | Посилання на TED |

## Scoring (0–100)

| Фактор | Макс | Логіка |
|--------|------|--------|
| CPV-match | 40 | Точний = 40, prefix = 25 |
| Keyword-match | 30 | 3+ слів = 30, 2 = 20, 1 = 10 |
| Бюджет | 20 | €50k–500k = 20, >500k = 10 |
| Дедлайн | 10 | ≤10 днів = 10, ≤30 = 5 |

## Налаштування Telegram

Скопіюй `.env.example` → `.env` і встанови токен бота:

```bash
cp .env.example .env
# Відкрий .env і встав TELEGRAM_BOT_TOKEN та TELEGRAM_USER_ID
```

Або через системні змінні:
```bash
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_USER_ID=...
```

## VPS cron (автоматизація)

```bash
# Щоденний повний DACH sweep (06:00 UTC)
0 6 * * * cd /opt/tender-scraper && python tender_scraper.py --region DACH --days 1 --quiet

# Тижневий дайджест (понеділок 08:00 UTC)
0 8 * * 1 cd /opt/tender-scraper && python tender_digest.py --region DACH --quiet
```

## CPV-коди (solar carport)

| Код | Назва |
|-----|-------|
| `09331200` | Solar photovoltaic modules |
| `09332000` | Solar installation |
| `45261215` | Solar panel roof-covering work |
| `44211000` | Prefabricated structures |
| `45223820` | Pre-fabricated units |
