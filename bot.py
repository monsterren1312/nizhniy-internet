#!/usr/bin/env python3
"""GTA 6 News Telegram Bot: t.me/s scraping -> Claude rewrite -> Telegram."""
import os, re, json, time, random, hashlib, logging, subprocess
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


def extract_media(m):
    """Возвращает (image_url, video_url) для сообщения t.me/s."""
    image = video = None
    ph = m.select_one(".tgme_widget_message_photo_wrap") or m.select_one(".tgme_widget_message_video_thumb")
    if ph:
        mm = re.search(r"url\('([^']+)'\)", ph.get("style", ""))
        if mm:
            image = mm.group(1)
    v = m.select_one("video")
    if v and v.get("src"):
        video = v["src"]
    return image, video


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
        image, video = extract_media(m)
        pid = int(post.split("/")[-1]) if post.split("/")[-1].isdigit() else 0
        items.append({"source": src["name"], "link": f"https://t.me/{post}",
                      "text": text[:1500], "image": image, "video": video, "id": pid,
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


def download(url, path, limit=45 * 1024 * 1024):
    try:
        with requests.get(url, headers=UA, stream=True, timeout=60) as r:
            r.raise_for_status()
            size = 0
            with open(path, "wb") as f:
                for chunk in r.iter_content(1024 * 256):
                    size += len(chunk)
                    if size > limit:
                        log.warning("Файл слишком большой, пропускаю медиа")
                        return False
                    f.write(chunk)
        return size > 0
    except Exception as e:
        log.warning(f"Не удалось скачать {url[:60]}: {e}")
        return False


def to_mp4(src, dst):
    try:
        r = subprocess.run(["ffmpeg", "-y", "-i", src, "-c:v", "libx264", "-preset", "veryfast",
                            "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart",
                            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", dst],
                           capture_output=True, timeout=600)
        return r.returncode == 0 and os.path.getsize(dst) < 49 * 1024 * 1024
    except Exception as e:
        log.warning(f"ffmpeg: {e}")
        return False


def tg_call(method, data, files=None):
    api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    for parse in ("Markdown", None):
        d = dict(data)
        if parse:
            d["parse_mode"] = parse
        if files:
            for f in files.values():
                f.seek(0)
        r = requests.post(api, data=d, files=files, timeout=300)
        if r.ok:
            return True
        log.warning(f"{method} ({parse}): {r.text[:200]}")
        if "parse" not in r.text.lower():
            break
    return False


def send_to_telegram(text, image=None, video=None):
    chat = {"chat_id": TELEGRAM_CHAT_ID}
    caption = text if len(text) <= 1024 else None
    sent_media = False
    try:
        if video:
            if download(video, "/tmp/in_video") and to_mp4("/tmp/in_video", "/tmp/out_video.mp4"):
                with open("/tmp/out_video.mp4", "rb") as f:
                    d = dict(chat, supports_streaming="true")
                    if caption:
                        d["caption"] = caption
                    sent_media = tg_call("sendVideo", d, {"video": f})
        if not sent_media and image:
            if download(image, "/tmp/in_image", limit=9 * 1024 * 1024):
                with open("/tmp/in_image", "rb") as f:
                    d = dict(chat)
                    if caption:
                        d["caption"] = caption
                    sent_media = tg_call("sendPhoto", d, {"photo": f})
        if sent_media and caption:
            return True
        if sent_media:
            return tg_call("sendMessage", dict(chat, text=text, disable_web_page_preview="true"))
        return tg_call("sendMessage", dict(chat, text=text, disable_web_page_preview="true"))
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
        if send_to_telegram(build_final_text(body), it["image"], it.get("video")):
            state["seen_links"].append(it["link"]); state["seen_hashes"].append(it["hash"])
            posted += 1
            schedule_next_post(state)
        else:
            log.error("Публикация не удалась")
    save_state(state)
    log.info(f"Готово. Опубликовано: {posted}")


if __name__ == "__main__":
    main()
