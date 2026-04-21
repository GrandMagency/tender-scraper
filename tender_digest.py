#!/usr/bin/env python3
"""
tender_digest.py — WAT Tool
Тижневий дайджест нових тендерів на solar carport.
Порівнює з попереднім запуском, надсилає тільки нові в Telegram.

Usage:
  python tender_digest.py
  python tender_digest.py --region DE --min-score 60
  python tender_digest.py --top 15
"""

import argparse
import csv
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path


# ── Telegram ───────────────────────────────────────────────────────────────────

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
            headers={"User-Agent": "WAT-tender-digest/1.0"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"WARN: Telegram помилка — {e}")


# ── State helpers ──────────────────────────────────────────────────────────────

STATE_FILE = Path(".state.json")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"tenders": {}, "last_digest_run": None, "digest_tenders": {}}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp.replace(STATE_FILE)


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


# ── Country emoji ──────────────────────────────────────────────────────────────

COUNTRY_FLAG = {"DE": "🇩🇪", "AT": "🇦🇹", "CH": "🇨🇭"}


def stars(score: int) -> str:
    if score >= 80:
        return "⭐⭐⭐"
    elif score >= 60:
        return "⭐⭐"
    return "⭐"


# ── Format digest ──────────────────────────────────────────────────────────────

def format_digest(region: str, new_records: list[dict], total_found: int,
                  top: int = 10) -> str:
    today = date.today().strftime("%d.%m.%Y")
    qualified = len(new_records)

    if qualified == 0:
        return (
            f"📋 <b>TENDER DIGEST — Solar Carport</b>\n"
            f"Регіон: {region} | {today}\n\n"
            f"✅ Нових тендерів не знайдено — база актуальна"
        )

    # Розбивка по країнах
    country_counts: dict[str, int] = {}
    for r in new_records:
        c = r.get("buyer_country", "?")
        country_counts[c] = country_counts.get(c, 0) + 1

    country_str = " | ".join(
        f"{COUNTRY_FLAG.get(c, c)} {c}: {n}"
        for c, n in sorted(country_counts.items())
    )

    # Топ-N по priority_score
    sorted_records = sorted(
        new_records, key=lambda r: -int(r.get("priority_score", 0))
    )
    top_records = sorted_records[:top]

    lines = [
        f"📋 <b>TENDER DIGEST — Solar Carport DACH</b>",
        f"Регіон: {region} | {today}",
        "",
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
        score = rec.get("priority_score", "?")
        email = rec.get("buyer_email", "")
        ted_url = rec.get("ted_url", "")

        lines.append(f"{i}. {stars(int(score) if str(score).isdigit() else 0)} "
                     f"{flag} <b>{buyer}</b>")
        lines.append(f"   📄 {title[:100]}")
        lines.append(f"   💰 {budget_str} | 📅 {deadline_str} | Score: {score}")
        if email:
            lines.append(f"   📧 {email}")
        if ted_url:
            lines.append(f'   🔗 <a href="{ted_url}">TED</a>')
        lines.append("")

    return "\n".join(lines)


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
    args = parser.parse_args()

    today = date.today().isoformat()
    region = args.region

    tmp_dir = Path(".tmp")
    tmp_dir.mkdir(exist_ok=True)
    output_dir = Path("output")
    output_dir.mkdir(exist_ok=True)

    tmp_csv = tmp_dir / f"tender_weekly_{region}_{today}.csv"
    final_csv = output_dir / f"tender_digest_{region}_{today}.csv"

    print(f"\n📋 Tender Digest — {region} | {today}")
    print(f"   min_score: {args.min_score}, days: {args.days}\n")

    # Запускаємо скрапер
    print("[1/1] Запит до TED API...")
    ok = run_scraper(
        region=region,
        days=args.days,
        min_score=0,  # беремо всі, фільтруємо нижче
        output=tmp_csv,
        quiet=True,
    )

    if not ok:
        tg_notify(f"❌ <b>Tender Digest — ПОМИЛКА</b>\nРегіон: {region}\nTED API не відповів")
        sys.exit(1)

    current_records = load_csv_records(tmp_csv)
    print(f"\n   Знайдено тендерів: {len(current_records)}")

    if not current_records:
        msg = format_digest(region, [], 0, args.top)
        tg_notify(msg)
        print("✓ Telegram дайджест надіслано (0 тендерів)")
        return

    # Відсіюємо ті що вже були в попередньому дайджесті
    state = load_state()
    prev_digest_ids = set(state.get("digest_tenders", {}).keys())

    new_records = [
        r for r in current_records
        if r.get("tender_id") not in prev_digest_ids
        and int(r.get("priority_score", 0)) >= args.min_score
    ]

    print(f"   Нових (не в попередньому дайджесті): {len(new_records)}")
    print(f"   Після фільтру score ≥ {args.min_score}: {len(new_records)}")

    # Зберегти digest CSV
    if new_records:
        sorted_new = sorted(new_records, key=lambda r: -int(r.get("priority_score", 0)))
        save_csv(sorted_new, final_csv)
        print(f"✓ Збережено → {final_csv}")

    # Оновити стан: додати всі поточні тендери в digest_tenders
    for r in current_records:
        tid = r.get("tender_id", "")
        if tid:
            state["digest_tenders"][tid] = r.get("notice_number", "")
    state["last_digest_run"] = today
    save_state(state)

    # Telegram
    msg = format_digest(region, new_records, len(current_records), args.top)
    tg_notify(msg)
    print("✓ Telegram дайджест надіслано")


if __name__ == "__main__":
    main()
