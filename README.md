# tender-scraper

Щоденний моніторинг публічних тендерів через офіційну базу ЄС (TED) + bund.de + SIMAP.
Результат: CSV з відфільтрованими тендерами + Telegram-сповіщення з топ-10.

**Хто використовує:** B2B компанії, які шукають гарячі ліди — організації з виділеним бюджетом,
що вже шукають постачальника через офіційний тендер.

---

## Швидкий старт (5 хвилин)

```bash
# 1. Клонуй репо
git clone https://github.com/GrandMagency/tender-scraper.git
cd tender-scraper

# 2. Конфігурація
cp .env.example .env
# Відкрий .env і заповни TELEGRAM_BOT_TOKEN + TELEGRAM_USER_ID

# 3. Запуск
python tender_scraper.py

# 4. Перевірка — CSV у папці output/
ls output/

# 5. Тижневий дайджест → Telegram
python tender_digest.py
```

Залежності: **тільки Python stdlib**. Нічого встановлювати не потрібно (Python 3.9+).

---

## Налаштування для нового клієнта

### Крок 1 — Telegram бот

1. Напиши [@BotFather](https://t.me/BotFather) → `/newbot` → отримай токен
2. Напиши своєму боту будь-що, потім відкрий: `https://api.telegram.org/bot<TOKEN>/getUpdates` → знайди свій `chat_id`
3. Встав у `.env`:
```
TELEGRAM_BOT_TOKEN=1234567890:AAF...
TELEGRAM_USER_ID=987654321
```

### Крок 2 — Ключові слова під нішу клієнта

Відкрий `tender_scraper.py`, рядки **57–87** — там `CARPORT_KEYWORDS`, `PARKING_PV_TERMS`, `SOLAR_CPV_PREFIXES`.

Замінити на ключові слова ніші клієнта. Приклади:

| Ніша | Ключові слова | CPV префікси |
|------|--------------|-------------|
| Solar carport (default) | Solarcarport, PV-Carport, Solardach | 09331200, 45261215 |
| IT-закупівлі | Software, IT-Dienstleistung, Digitalisierung | 72000000, 48000000 |
| Будівництво | Hochbau, Tiefbau, Sanierung | 45000000, 45210000 |
| Медтехніка | Medizintechnik, Diagnostik, MRT | 33100000, 33120000 |
| Охорона | Sicherheitsdienst, Bewachung | 79710000, 79711000 |

### Крок 3 — Регіон (опціонально)

Default: DACH (DE + AT + CH). Можна змінити через CLI:
```bash
python tender_scraper.py --region DE        # тільки Німеччина
python tender_scraper.py --region EU        # вся ЄС
```

---

## Підключення бота — три рівні

Tender scraper працює **без AI** (тільки rule-based scoring). AI додається опціонально для розумнішого ранжування і генерації аутричу.

### Level 0 — Без AI (out of the box)
Потрібно: тільки `TELEGRAM_BOT_TOKEN` + `TELEGRAM_USER_ID`.
Скорингова система — правила: CPV коди + ключові слова + бюджет + дедлайн.
Запускається одразу після заповнення `.env`.

### Level 1 — Claude Code (Max login)
Для розробників з Max-підпискою claude.ai.
Використання: запускати аналіз через `claude` CLI у директорії скрапера.
Вартість: включено в Max-підписку.
```bash
# Приклад: попросити Claude проаналізувати топ-тендери
claude "Переглянь output/tenders_dach_latest.csv і напиши топ-3 листи для outreach"
```

### Level 2 — Claude API (ANTHROPIC_API_KEY)
Автономний режим без інтерактивного сеансу.
```
ANTHROPIC_API_KEY=sk-ant-...
```
Модель: `claude-sonnet-4-6` (оптимальний баланс якість/вартість).
Використання: для нічного аналізу, генерації email, автоматичного ранжування.

### Level 3 — OpenAI GPT (OPENAI_API_KEY)
Альтернатива для клієнтів без доступу до Claude.
```
OPENAI_API_KEY=sk-...
```
Модель: `gpt-5.4-mini` (швидко, дешево, достатньо для аналізу тендерів).

---

## Автоматизація (VPS cron)

```bash
# Відкрий crontab
crontab -e

# Додай рядки:
# Щоденний скан (06:00 UTC)
0 6 * * * cd /opt/tender-scraper && python tender_scraper.py --region DACH --days 1 --quiet

# Тижневий дайджест (понеділок 08:00 UTC)
0 8 * * 1 cd /opt/tender-scraper && python tender_digest.py --region DACH --quiet
```

---

## CLI — повний список аргументів

### `tender_scraper.py`

| Аргумент | Default | Опис |
|----------|---------|------|
| `--region` | `DACH` | `DE`, `AT`, `CH`, `DACH`, `EU` |
| `--days` | `7` | Кількість днів назад |
| `--min-score` | `30` | Мінімальний пріоритет 0–100 |
| `--output` | авто | Шлях до CSV |
| `--quiet` | — | Без verbose |
| `--no-telegram` | — | Без Telegram-сповіщень |
| `--dry-run` | — | Показати запит без виконання |

### `tender_digest.py`

| Аргумент | Default | Опис |
|----------|---------|------|
| `--region` | `DACH` | Регіон |
| `--min-score` | `50` | Поріг для дайджесту |
| `--days` | `7` | Діапазон пошуку |
| `--top` | `10` | Кількість тендерів у Telegram |

---

## Що в CSV

| Колонка | Опис |
|---------|------|
| `tender_id` | Унікальний ID (dedup ключ) |
| `notice_number` | Номер TED (напр. `2026/S 123-456789`) |
| `title` | Назва тендера |
| `buyer_name` | Назва замовника |
| `buyer_country` | DE / AT / CH |
| `buyer_email` | Email для контакту |
| `description` | Перші 500 символів опису |
| `estimated_value_eur` | Бюджет в EUR |
| `deadline_date` | Дедлайн подачі |
| `days_until_deadline` | Днів до дедлайну |
| `priority_score` | Пріоритет 0–100 |
| `ted_url` | Посилання на тендер |

---

## Файли

```
tender-scraper/
├── tender_scraper.py    # головний скрапер (TED + bund.de + SIMAP)
├── tender_digest.py     # тижневий дайджест → Telegram
├── tender_viewer.html   # локальний браузер CSV (відкрити у браузері)
├── .env.example         # шаблон конфігурації
├── requirements.txt     # порожній — тільки stdlib
├── output/              # CSV виходи (gitignored)
└── .state.json          # dedup стан між запусками (gitignored)
```

---

## Перелік того що треба зробити для нового клієнта

- [ ] Створити Telegram бота (@BotFather) — отримати токен
- [ ] Заповнити `.env` (токен + chat_id)
- [ ] Замінити ключові слова і CPV коди (рядки 57–87 у `tender_scraper.py`)
- [ ] Тест запуску: `python tender_scraper.py --dry-run`
- [ ] Тест результату: `python tender_scraper.py --region DE --days 30 --no-telegram`
- [ ] Перевірити CSV — чи є релевантні тендери
- [ ] Налаштувати cron на VPS
- [ ] Перший реальний запуск з Telegram
- [ ] (Опціонально) Додати API ключ для AI-аналізу (Level 2 або 3)

---

*GrandMa Agency · grandma.agency*
