#!/usr/bin/env bash
# verify.sh — самотест tender-scraper
# Запускати після будь-яких змін: bash verify.sh
# Exit 0 = все OK, Exit 1 = щось зламалось

set -euo pipefail
PASS=0; FAIL=0
DIR="$(cd "$(dirname "$0")" && pwd)"

ok()   { echo "  ✅ $1"; PASS=$((PASS+1)); }
fail() { echo "  ❌ $1"; FAIL=$((FAIL+1)); }
hdr()  { echo ""; echo "── $1 ──────────────────────────────"; }

echo "🔍 tender-scraper verify — $(date '+%Y-%m-%d %H:%M')"

# ── 1. Python синтаксис ──────────────────────────────────────────────────────
hdr "Синтаксис"
python3 -m py_compile "$DIR/tender_scraper.py" 2>/dev/null \
  && ok "tender_scraper.py" || fail "tender_scraper.py — синтаксична помилка"
python3 -m py_compile "$DIR/tender_digest.py" 2>/dev/null \
  && ok "tender_digest.py" || fail "tender_digest.py — синтаксична помилка"

# ── 2. config.json ───────────────────────────────────────────────────────────
hdr "config.json"
if [[ -f "$DIR/config.json" ]]; then
  python3 -c "import json; json.load(open('$DIR/config.json'))" 2>/dev/null \
    && ok "config.json валідний JSON" || fail "config.json — невалідний JSON"
  for key in keywords cpv_codes simap_terms dab_terms; do
    python3 -c "import json; d=json.load(open('$DIR/config.json')); assert '$key' in d" 2>/dev/null \
      && ok "  → '$key' присутній" || fail "  → '$key' відсутній"
  done
else
  fail "config.json не існує"
fi

# ── 3. .env змінні ───────────────────────────────────────────────────────────
hdr ".env"
ENV_FILE="$DIR/.env"
if [[ -f "$ENV_FILE" ]]; then
  ok ".env існує"
  for var in TELEGRAM_BOT_TOKEN TELEGRAM_USER_ID; do
    grep -q "^${var}=" "$ENV_FILE" \
      && ok "  → $var встановлено" || fail "  → $var відсутній у .env"
  done
  grep -q "^TELEGRAM_BOT_TOKEN=.\{5\}" "$ENV_FILE" \
    && ok "  → TELEGRAM_BOT_TOKEN не порожній" || fail "  → TELEGRAM_BOT_TOKEN порожній"
else
  fail ".env не існує (скопіюй з .env.example)"
fi

# ── 4. Симлінк ───────────────────────────────────────────────────────────────
hdr "Симлінк"
if [[ -L "/opt/tender-scraper" ]]; then
  TARGET=$(readlink /opt/tender-scraper)
  REAL=$(realpath /opt/tender-scraper 2>/dev/null)
  ok "/opt/tender-scraper → $TARGET"
  [[ -d "$REAL" ]] && ok "  → симлінк resolve OK" || fail "  → симлінк broken (target не існує)"
else
  echo "  ℹ️  симлінк /opt/tender-scraper відсутній (не обов'язково)"
fi

# ── 5. Hub env (multi-recipient) ─────────────────────────────────────────────
hdr "Hub (multi-recipient)"
HUB_PATH="${TENDER_DIGEST_HUB_ENV:-/opt/client-hub-bot/.env}"
if [[ -f "$HUB_PATH" ]]; then
  ok "hub .env знайдено: $HUB_PATH"
  grep -q "ALLOWED_USER_IDS\|TENDER_DIGEST_USER_IDS" "$HUB_PATH" \
    && ok "  → отримувачі прописані" || fail "  → ALLOWED_USER_IDS не знайдено"
else
  echo "  ℹ️  hub .env не знайдено ($HUB_PATH) — тендери підуть тільки на TELEGRAM_USER_ID"
fi

# ── 6. Output директорія ─────────────────────────────────────────────────────
hdr "Output"
mkdir -p "$DIR/output" 2>/dev/null && ok "output/ існує і доступна для запису" \
  || fail "output/ — нема доступу на запис"

# ── 7. Dry-run ───────────────────────────────────────────────────────────────
hdr "Dry-run"
cd "$DIR"
if python3 tender_scraper.py --dry-run --no-telegram 2>&1 | grep -q "Dry-run"; then
  ok "--dry-run пройшов"
else
  fail "--dry-run впав"
fi

# ── Результат ─────────────────────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════"
if [[ $FAIL -eq 0 ]]; then
  echo "✅ OK — $PASS перевірок пройшло"
  exit 0
else
  echo "❌ FAIL — $FAIL помилок / $PASS пройшло"
  exit 1
fi
