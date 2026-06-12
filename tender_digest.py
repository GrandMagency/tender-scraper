#!/usr/bin/env python3
"""
tender_digest.py — WAT Tool
Тижневий дайджест нових тендерів на solar carport.
Порівнює з попереднім запуском, надсилає тільки нові в Telegram.

Опціональний AI-шар (ANTHROPIC_API_KEY або OPENAI_API_KEY у .env):
для топ-тендерів генерує пояснення релевантності (укр.) у дайджест
і чернетки outreach-листів (нім.) у output/tender_outreach_*.md.

Usage:
  python tender_digest.py
  python tender_digest.py --region DE --min-score 60
  python tender_digest.py --top 15 --no-telegram
"""

import argparse
import csv
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

# Спільні зі скрапером: стан (з TTL-очисткою) і версія User-Agent
from tender_scraper import load_state, save_state

__version__ = "1.1.0"

# ── AI (опціонально) ───────────────────────────────────────────────────────────

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
AI_MODEL_ANTHROPIC = os.environ.get("TENDER_AI_MODEL", "claude-haiku-4-5")
AI_MODEL_OPENAI = os.environ.get("OPENAI_MODEL", "gpt-5.4-mini")
AI_MAX_ITEMS = 10  # скільки топ-тендерів анотуємо за запуск

_AI_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tender_id": {"type": "string"},
                    "why": {"type": "string",
                            "description": "1-2 речення українською: чому тендер релевантний для профілю"},
                    "email_subject": {"type": "string",
                                      "description": "Тема outreach-листа німецькою"},
                    "email_body": {"type": "string",
                                   "description": "Тіло outreach-листа німецькою, 100-150 слів, діловий тон"},
                },
                "required": ["tender_id", "why", "email_subject", "email_body"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["items"],
    "additionalProperties": False,
}


def _ai_prompt(records: list[dict], profile: str) -> str:
    tenders = [
        {
            "tender_id": r.get("tender_id", ""),
            "title": (r.get("title") or "")[:200],
            "buyer": (r.get("buyer_name") or "")[:120],
            "country": r.get("buyer_country", ""),
            "budget_eur": r.get("estimated_value_eur", ""),
            "deadline": r.get("deadline_date", ""),
            "score": r.get("priority_score", ""),
        }
        for r in records
    ]
    return (
        f"Ти — асистент B2B-компанії з профілем: «{profile}».\n"
        f"Нижче список публічних тендерів (JSON). Для КОЖНОГО тендера поверни:\n"
        f"- why: 1-2 речення українською, чому цей тендер релевантний (або в чому ризик/нюанс);\n"
        f"- email_subject: тема короткого outreach-листа німецькою до замовника;\n"
        f"- email_body: тіло листа німецькою, 100-150 слів, діловий тон, без вигаданих фактів "
        f"про компанію — загальне представлення компетенції за профілем, посилання на конкретний "
        f"тендер за назвою, пропозиція короткого дзвінка.\n\n"
        f"Тендери:\n{json.dumps(tenders, ensure_ascii=False, indent=1)}"
    )


def _ai_annotate_anthropic(records: list[dict], profile: str, api_key: str,
                           verbose: bool) -> dict[str, dict]:
    body = json.dumps({
        "model": AI_MODEL_ANTHROPIC,
        "max_tokens": 8000,
        "messages": [{"role": "user", "content": _ai_prompt(records, profile)}],
        "output_config": {"format": {"type": "json_schema", "schema": _AI_SCHEMA}},
    }).encode("utf-8")
    req = urllib.request.Request(
        ANTHROPIC_API_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "User-Agent": f"WAT-tender-digest/{__version__}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("stop_reason") not in ("end_turn", "stop_sequence"):
        if verbose:
            print(f"  [AI] неочікуваний stop_reason: {data.get('stop_reason')} — пропускаю")
        return {}
    text = next((b["text"] for b in data.get("content", []) if b.get("type") == "text"), "")
    items = json.loads(text).get("items", [])
    return {i["tender_id"]: i for i in items if i.get("tender_id")}


def _ai_annotate_openai(records: list[dict], profile: str, api_key: str,
                        verbose: bool) -> dict[str, dict]:
    prompt = _ai_prompt(records, profile) + (
        '\n\nВідповідь — строго JSON: {"items": [{"tender_id", "why", '
        '"email_subject", "email_body"}]}'
    )
    body = json.dumps({
        "model": AI_MODEL_OPENAI,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
    }).encode("utf-8")
    req = urllib.request.Request(
        OPENAI_API_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": f"WAT-tender-digest/{__version__}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    text = data["choices"][0]["message"]["content"]
    items = json.loads(text).get("items", [])
    return {i["tender_id"]: i for i in items if i.get("tender_id")}


def ai_annotate(records: list[dict], profile: str, verbose: bool = True) -> dict[str, dict]:
    """AI-аналіз топ-тендерів. Повертає {tender_id: {why, email_subject, email_body}}.

    Без API-ключа тихо пропускається (Level 0 працює як раніше).
    """
    if not records:
        return {}
    records = records[:AI_MAX_ITEMS]
    try:
        if os.environ.get("ANTHROPIC_API_KEY"):
            if verbose:
                print(f"  [AI] Анотація {len(records)} тендерів через {AI_MODEL_ANTHROPIC}...")
            return _ai_annotate_anthropic(records, profile,
                                          os.environ["ANTHROPIC_API_KEY"], verbose)
        if os.environ.get("OPENAI_API_KEY"):
            if verbose:
                print(f"  [AI] Анотація {len(records)} тендерів через {AI_MODEL_OPENAI}...")
            return _ai_annotate_openai(records, profile,
                                       os.environ["OPENAI_API_KEY"], verbose)
        if verbose:
            print("  [AI] API-ключ не задано (ANTHROPIC_API_KEY/OPENAI_API_KEY) — без AI-аналізу")
    except Exception as e:
        print(f"WARN: AI-аналіз не вдався — {e}", file=sys.stderr)
    return {}


def save_outreach_md(annotations: dict[str, dict], records: list[dict],
                     path: Path) -> None:
    """Чернетки outreach-листів у Markdown (для копіювання в пошту)."""
    by_id = {r.get("tender_id"): r for r in records}
    lines = [f"# Outreach-чернетки — Tender Digest {date.today().isoformat()}", ""]
    for tid, ann in annotations.items():
        rec = by_id.get(tid, {})
        lines += [
            f"## {rec.get('buyer_name', tid)}",
            f"- Тендер: {rec.get('title', '—')}",
            f"- Дедлайн: {rec.get('deadline_date', '—')} | Score: {rec.get('priority_score', '—')}",
            f"- Email: {rec.get('buyer_email') or '— (немає, шукати вручну)'}",
            f"- Лінк: {rec.get('ted_url', '—')}",
            f"- Чому релевантно: {ann.get('why', '')}",
            "",
            f"**Betreff:** {ann.get('email_subject', '')}",
            "",
            ann.get("email_body", ""),
            "",
            "---",
            "",
        ]
    lines.append(f"*tender-digest v{__version__} · GrandMa Agency · grandma.agency*")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


# ── Telegram ───────────────────────────────────────────────────────────────────

TG_MAX_LEN = 4096  # ліміт Telegram на повідомлення


def tg_notify(msg: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_USER_ID")
    if not token or not chat_id:
        print("WARN: TELEGRAM_BOT_TOKEN або TELEGRAM_USER_ID не встановлено")
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
            headers={"User-Agent": f"WAT-tender-digest/{__version__}"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"WARN: Telegram помилка — {e}")


# ── CSV helpers ────────────────────────────────────────────────────────────────

def load_csv_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_csv(records: list[dict], path: Path) -> None:
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(records[0].keys()))
        writer.writeheader()
        writer.writerows(records)


# ── Run scraper ────────────────────────────────────────────────────────────────

def run_scraper(region: str, days: int, min_score: int,
                output: Path, quiet: bool = True) -> bool:
    """Запускає tender_scraper.py і повертає True якщо успішно."""
    script = Path(__file__).parent / "tender_scraper.py"
    cmd = [
        sys.executable, str(script),
        "--region", region,
        "--days", str(days),
        "--min-score", str(min_score),
        "--no-telegram",
        "--no-dedup",
        "--output", str(output),
    ]
    if quiet:
        cmd.append("--quiet")

    print(f"  → {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"  ERROR: {result.stderr[:300]}")
        return False
    if not quiet and result.stdout:
        print(result.stdout)
    return True


# ── Format digest ──────────────────────────────────────────────────────────────

COUNTRY_FLAG = {"DE": "🇩🇪", "AT": "🇦🇹", "CH": "🇨🇭"}

EXPIRING_MIN_SCORE = 70   # нагадування: гарячий тендер...
EXPIRING_MAX_DAYS = 7     # ...у якого дедлайн ≤ 7 днів


def safe_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def stars(score: int) -> str:
    if score >= 80:
        return "⭐⭐⭐"
    elif score >= 60:
        return "⭐⭐"
    return "⭐"


def find_expiring(records: list[dict], prev_ids: set[str]) -> list[dict]:
    """Гарячі (score ≥ 70) уже відомі тендери з дедлайном ≤ 7 днів — щоб не згоріли."""
    expiring = []
    for r in records:
        days_left = safe_int(r.get("days_until_deadline"), default=-1)
        if (r.get("tender_id") in prev_ids
                and safe_int(r.get("priority_score")) >= EXPIRING_MIN_SCORE
                and 0 <= days_left <= EXPIRING_MAX_DAYS):
            expiring.append(r)
    return sorted(expiring, key=lambda r: safe_int(r.get("days_until_deadline"), 99))


def format_digest(region: str, new_records: list[dict], total_found: int,
                  top: int = 10, annotations: dict[str, dict] | None = None,
                  expiring: list[dict] | None = None) -> str:
    today = date.today().strftime("%d.%m.%Y")
    qualified = len(new_records)
    annotations = annotations or {}
    footer = f"\n<i>tender-digest v{__version__} · GrandMa Agency · grandma.agency</i>"

    lines = [
        f"📋 <b>TENDER DIGEST — Solar Carport</b>",
        f"Регіон: {region} | {today}",
        "",
    ]

    if qualified == 0:
        lines.append("✅ Нових тендерів не знайдено — база актуальна")
    else:
        country_counts: dict[str, int] = {}
        for r in new_records:
            c = r.get("buyer_country", "?")
            country_counts[c] = country_counts.get(c, 0) + 1
        country_str = " | ".join(
            f"{COUNTRY_FLAG.get(c, c)} {c}: {n}"
            for c, n in sorted(country_counts.items())
        )
        sorted_records = sorted(
            new_records, key=lambda r: -safe_int(r.get("priority_score"))
        )
        top_records = sorted_records[:top]

        lines += [
            f"🆕 Всього знайдено: {total_found} | ✅ Нових (score ≥ поріг): <b>{qualified}</b>",
            f"{country_str}",
            "",
            f"🔥 ТОП {len(top_records)}:",
            "",
        ]

        for i, rec in enumerate(top_records, 1):
            country = rec.get("buyer_country", "?")
            flag = COUNTRY_FLAG.get(country, "🌍")
            title = rec.get("title") or rec.get("notice_number") or "—"
            buyer = rec.get("buyer_name") or "—"
            value = rec.get("estimated_value_eur", "")
            budget_str = f"€{int(float(value)):,}" if value else "бюджет невідомий"
            deadline = rec.get("deadline_date", "")
            days_left = rec.get("days_until_deadline", "")
            deadline_str = f"{deadline} ({days_left} дн.)" if deadline and days_left else deadline or "—"
            score = safe_int(rec.get("priority_score"))
            email = rec.get("buyer_email", "")
            ted_url = rec.get("ted_url", "")
            ann = annotations.get(rec.get("tender_id", ""))

            lines.append(f"{i}. {stars(score)} {flag} <b>{buyer}</b>")
            lines.append(f"   📄 {title[:100]}")
            lines.append(f"   💰 {budget_str} | 📅 {deadline_str} | Score: {score}")
            if ann and ann.get("why"):
                lines.append(f"   💡 {ann['why'][:250]}")
            if email:
                lines.append(f"   📧 {email}")
            if ted_url:
                lines.append(f'   🔗 <a href="{ted_url}">Лінк</a>')
            lines.append("")

    if expiring:
        lines += ["", f"🔚 <b>Згорають (дедлайн ≤ {EXPIRING_MAX_DAYS} дн., score ≥ {EXPIRING_MIN_SCORE}):</b>"]
        for rec in expiring[:5]:
            lines.append(
                f"• {rec.get('buyer_name', '—')} — {(rec.get('title') or '')[:60]} "
                f"({rec.get('days_until_deadline', '?')} дн.)"
            )

    msg = "\n".join(lines) + footer
    # Telegram ріже повідомлення > 4096 символів — зменшуємо топ, поки не влізе
    if len(msg) > TG_MAX_LEN and top > 3:
        return format_digest(region, new_records, total_found, top - 2,
                             annotations, expiring)
    return msg[:TG_MAX_LEN]


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Tender Digest — тижневий звіт в Telegram")
    parser.add_argument(
        "--region",
        default="DACH",
        choices=["DE", "AT", "CH", "DACH"],
        help="Регіон (default: DACH)",
    )
    parser.add_argument(
        "--min-score",
        type=int,
        default=50,
        help="Мінімальний score для дайджесту (default: 50)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Кількість днів назад (default: 7)",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="Скільки топ-тендерів в Telegram (default: 10)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Без verbose виводу",
    )
    parser.add_argument(
        "--no-telegram",
        action="store_true",
        help="Не надсилати в Telegram (тільки CSV/outreach файли)",
    )
    parser.add_argument(
        "--no-ai",
        action="store_true",
        help="Пропустити AI-аналіз навіть якщо є API-ключ",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"tender-digest {__version__}",
    )
    args = parser.parse_args()

    today = date.today().isoformat()
    region = args.region

    tmp_dir = Path(".tmp")
    tmp_dir.mkdir(exist_ok=True)
    output_dir = Path("output")
    output_dir.mkdir(exist_ok=True)

    tmp_csv = tmp_dir / f"tender_weekly_{region}_{today}.csv"
    final_csv = output_dir / f"tender_digest_{region}_{today}.csv"

    print(f"\n📋 Tender Digest v{__version__} — {region} | {today}")
    print(f"   min_score: {args.min_score}, days: {args.days}\n")

    # Запускаємо скрапер
    print("[1/3] Збір тендерів (усі джерела)...")
    ok = run_scraper(
        region=region,
        days=args.days,
        min_score=0,  # беремо всі, фільтруємо нижче
        output=tmp_csv,
        quiet=True,
    )

    if not ok:
        if not args.no_telegram:
            tg_notify(f"❌ <b>Tender Digest — ПОМИЛКА</b>\nРегіон: {region}\nСкрапер завершився з помилкою")
        sys.exit(1)

    current_records = load_csv_records(tmp_csv)
    print(f"\n   Знайдено тендерів: {len(current_records)}")

    state = load_state()
    prev_digest_ids = set(state.get("digest_tenders", {}).keys())

    if not current_records:
        msg = format_digest(region, [], 0, args.top)
        if not args.no_telegram:
            tg_notify(msg)
            print("✓ Telegram дайджест надіслано (0 тендерів)")
        return

    # Відсіюємо ті що вже були в попередньому дайджесті
    new_records = [
        r for r in current_records
        if r.get("tender_id") not in prev_digest_ids
        and safe_int(r.get("priority_score")) >= args.min_score
    ]
    expiring = find_expiring(current_records, prev_digest_ids)

    print(f"   Нових (не в попередньому дайджесті, score ≥ {args.min_score}): {len(new_records)}")
    if expiring:
        print(f"   🔚 Згорають (дедлайн ≤ {EXPIRING_MAX_DAYS} дн.): {len(expiring)}")

    sorted_new = sorted(new_records, key=lambda r: -safe_int(r.get("priority_score")))

    # AI-аналіз топ-тендерів (опціонально)
    annotations: dict[str, dict] = {}
    if not args.no_ai and sorted_new:
        print("[2/3] AI-аналіз...")
        profile = _load_profile()
        annotations = ai_annotate(sorted_new[:args.top], profile, verbose=not args.quiet)
        if annotations:
            outreach_path = output_dir / f"tender_outreach_{region}_{today}.md"
            save_outreach_md(annotations, sorted_new, outreach_path)
            print(f"✓ Outreach-чернетки → {outreach_path}")

    # Зберегти digest CSV
    if sorted_new:
        save_csv(sorted_new, final_csv)
        print(f"✓ Збережено → {final_csv}")

    # Оновити стан: додати всі поточні тендери в digest_tenders
    for r in current_records:
        tid = r.get("tender_id", "")
        if tid:
            state["digest_tenders"][tid] = r.get("notice_number", "") or r.get("publication_number", "")
            state["tender_dates"].setdefault(tid, today)
    state["last_digest_run"] = today
    save_state(state)

    # Telegram
    print("[3/3] Надсилання...")
    msg = format_digest(region, new_records, len(current_records), args.top,
                        annotations, expiring)
    if args.no_telegram:
        print("— Telegram пропущено (--no-telegram). Прев'ю:\n")
        print(msg)
    else:
        tg_notify(msg)
        print("✓ Telegram дайджест надіслано")


def _load_profile() -> str:
    cfg_path = Path(__file__).parent / "config.json"
    try:
        with open(cfg_path, encoding="utf-8") as f:
            return json.load(f).get("profile", "B2B-постачальник")
    except Exception:
        return "B2B-постачальник"


if __name__ == "__main__":
    main()
