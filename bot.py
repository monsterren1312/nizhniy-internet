#!/usr/bin/env python3
"""GTA 6 News Telegram Bot: t.me/s scraping -> Claude rewrite -> Telegram."""
import os, re, json, time, random, hashlib, logging
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup
from anthropic import Anthropic

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("gta6-bot")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
MAX_POSTS_PER_RUN = int(os.environ.get("MAX_POSTS_PER_RUN", "1"))
MIN_INTERVAL_MINUTES = int(os.environ.get("MIN_INTERVAL_MINUTES", "45"))
MAX_INTERVAL_MINUTES = int(os.environ.get("MAX_INTERVAL_MINUTES", "120"))
STATE_FILE = os.environ.get("STATE_FILE", "state/seen.json")
CHANNEL_SIGNATURE = os.environ.get("CHANNEL_SIGNATURE", "🎮 GTA 6 Новости")
CHANNEL_URL = os.environ.get("CHANNEL_URL", "")

SOURCES = [
    {"name": "Мир GTA", "channel": "mir_gta6"},
    {"name": "GTA 6 Deluxe", "channel": "gta6deluxe"},
]

KEYWORDS = [
    "gta 6", "gta6", "gta vi", "gta-6", "гта 6", "гта6", "гта vi", "гта-6",
    "vice city", "вайс-сити", "вайс сити", "лусия", "lucia", "джейсон", "jason",
    "леонида", "leonida", "rockstar", "рокстар", "take-two", "take two", "тейк-ту",
    "трейлер", "trailer", "релиз", "дата выхода", "перенос", "скриншот",
    "ps5", "xbox series", "консол", "геймплей", "gameplay", "gta online",
    "зеленая", "зелёная", "хаузер", "houser", "утечка", "слив", "предзаказ",
]

client = Anthropic(api_key=ANTHROPIC_API_KEY)
UA = {"User-Agent": "Mozilla/5.0 (compatible; Mozilla/5.0)"}


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"seen_hashes": [], "seen_links": [], "next_post_not_before": None}
    with open(STATE_FILE, encoding="utf-8") as f:
        s = json.load(f)
    s.setdefault("seen_hashes", []); s.setdefault("seen_links", [])
    s.setdefault("next_post_not_before", None)
    return s


def save_state(s):
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    s["seen_hashes"] = s["seen_hashes"][-500:]
    s["seen_links"] = s["seen_links"][-500:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)


def schedule_next_post(s):
    d = random.uniform(MIN_INTERVAL_MINUTES, MAX_INTERVAL_MINUTES)
    s["next_post_not_before"] = datetime.now(timezone.utc).timestamp() + d * 60
    log.info(f"Следующий пост не раньше чем через {d:.1f} мин")


def is_too_early(s):
    nb = s.get("next_post_not_before")
    return nb is not None and datetime.now(timezone.utc).timestamp() < nb


def content_hash(text):
    n = "".join(c for c in text.lower() if c.isalnum() or c.isspace())
    return hashlib.sha256(" ".join(n.split())[:400].encode("utf-8")).hexdigest()


def clean_text(text):
    out = []
    for line in text.splitlines():
        l = line.strip()
        if not l:
            out.append("")
            continue
        low = l.lower()
        if "подписаться" in low or "наш чат" in low or "t.me/" in low:
            continue
        if l.startswith("🔫"):
            continue
        out.append(l)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def parse_channel(src):
    r = requests.get(f"https://t.me/s/{src['channel']}", headers=UA, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    for m in soup.select(".tgme_widget_message"):
        post = m.get("data-post")
        t = m.select_one(".tgme_widget_message_text")
        if not post or not t:
            continue
        for e in t.find_all("tg-emoji"):
            b = e.find("b")
            e.replace_with(b.get_text() if b else "")
        for br in t.find_all("br"):
            br.replace_with("\n")
        text = clean_text(t.get_text())
        if len(text) < 40:
            continue
        image = None
        ph = m.select_one(".tgme_widget_message_photo_wrap")
        if ph:
            mm = re.search(r"url\('([^']+)'\)", ph.get("style", ""))
            if mm:
                image = mm.group(1)
        pid = int(post.split("/")[-1]) if post.split("/")[-1].isdigit() else 0
        items.append({"source": src["name"], "link": f"https://t.me/{post}",
                      "text": text[:1500], "image": image, "id": pid,
                      "hash": content_hash(text)})
    return items


def fetch_candidates(state):
    cands = []
    for src in SOURCES:
        try:
            items = parse_channel(src)
        except Exception as e:
            log.warning(f"Не удалось загрузить {src['name']}: {e}")
            continue
        log.info(f"{src['name']}: {len(items)} постов с текстом")
        for it in items[-15:]:
            if it["link"] in state["seen_links"] or it["hash"] in state["seen_hashes"]:
                continue
            low = it["text"].lower()
            if not any(k in low for k in KEYWORDS):
                continue
            cands.append(it)
    cands.sort(key=lambda c: c["id"], reverse=True)
    return cands


def rewrite(text, source):
    prompt = f"""Ты редактор Telegram-канала про GTA 6 на русском языке.

Исходный пост из канала «{source}»:

{text}

Перепиши его своими словами для нашего канала:
- Первая строка: жирный заголовок в Markdown (*так*), один подходящий эмодзи в начале.
- Затем 2-4 коротких предложения только с фактами из поста, без выдумок.
- Если в посте слух, утечка или инсайд, прямо напиши, что это неподтверждённо.
- Без упоминания исходного канала, без ссылок, хэштегов, подписей и призывов подписаться.
- Не копируй формулировки дословно. Максимум 600 символов.
- Если в посте нет новости про GTA 6 или Rockstar (реклама, опрос, мем), ответь одним словом: SKIP.

Ответь только готовым текстом."""
    try:
        r = client.messages.create(model="claude-sonnet-4-6", max_tokens=500,
                                   messages=[{"role": "user", "content": prompt}])
        t = "".join(b.text for b in r.content if hasattr(b, "text")).strip()
        return None if not t or t.upper().startswith("SKIP") else t
    except Exception as e:
        log.error(f"Ошибка Anthropic API: {e}")
        return None


def build_final_text(body):
    sig = f"[{CHANNEL_SIGNATURE}]({CHANNEL_URL})" if CHANNEL_URL else CHANNEL_SIGNATURE
    return f"{body}\n\n{sig}"


def send_to_telegram(text, image):
    api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    try:
        if image and len(text) <= 1024:
            r = requests.post(f"{api}/sendPhoto", data={
                "chat_id": TELEGRAM_CHAT_ID, "photo": image, "caption": text,
                "parse_mode": "Markdown"}, timeout=30)
            if r.ok:
                return True
            log.warning(f"sendPhoto не удался: {r.text[:200]}")
        r = requests.post(f"{api}/sendMessage", data={
            "chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown",
            "disable_web_page_preview": True}, timeout=30)
        if r.ok:
            return True
        r = requests.post(f"{api}/sendMessage", data={
            "chat_id": TELEGRAM_CHAT_ID, "text": text,
            "disable_web_page_preview": True}, timeout=30)
        if not r.ok:
            log.error(f"sendMessage: {r.text[:300]}")
        return r.ok
    except Exception as e:
        log.error(f"Ошибка Telegram: {e}")
        return False


def main():
    log.info("Запуск GTA 6 бота")
    state = load_state()
    if is_too_early(state):
        log.info("Ещё не время для следующего поста")
        return
    cands = fetch_candidates(state)
    log.info(f"Кандидатов: {len(cands)}")
    posted = 0
    for it in cands:
        if posted >= MAX_POSTS_PER_RUN:
            break
        log.info(f"Обрабатываю: [{it['source']}] {it['text'][:70]}")
        body = rewrite(it["text"], it["source"])
        if not body:
            state["seen_links"].append(it["link"]); state["seen_hashes"].append(it["hash"])
            continue
        if send_to_telegram(build_final_text(body), it["image"]):
            state["seen_links"].append(it["link"]); state["seen_hashes"].append(it["hash"])
            posted += 1
            schedule_next_post(state)
        else:
            log.error("Публикация не удалась")
    save_state(state)
    log.info(f"Готово. Опубликовано: {posted}")


if __name__ == "__main__":
    main()
