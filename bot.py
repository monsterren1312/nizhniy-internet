#!/usr/bin/env python3
"""
Нижний интернет — бот видео-канала с ручным одобрением.

Каждый запуск (GitHub Actions, раз в 15 минут):
1. Читает нажатия кнопок ✅/❌ в личке админа и обновляет очередь.
2. Если пора — публикует следующее одобренное видео в канал.
3. Если кандидатов на проверке мало — берёт свежие вирусные видео с Reddit,
   Claude отбирает самые дикие и пишет подпись, бот присылает их админу на одобрение.
"""

import os
import re
import json
import time
import glob
import logging
import subprocess
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from anthropic import Anthropic

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("nizhniy-internet")

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
CHANNEL_ID = os.environ["TELEGRAM_CHAT_ID"].strip()
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"].strip())
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"].strip()

SUBREDDITS = [
    "WTF", "PublicFreakout", "Unexpected", "instant_regret", "interestingasfuck",
    "nextfuckinglevel", "Whatcouldgowrong", "therewasanattempt", "IdiotsInCars",
    "oddlyterrifying", "holdmybeer", "BeAmazed",
]

MAX_PENDING = 5            # сколько видео одновременно ждут вашего решения
MAX_CANDIDATES_PER_DAY = 15
MAX_POSTS_PER_DAY = 6
MIN_GAP_MINUTES = 90       # минимум между постами в канале
POST_HOURS = (9, 23)       # публикуем с 9:00 до 23:00 по Германии
MAX_DURATION_SEC = 120
MAX_FILE_MB = 48           # лимит Telegram Bot API — 50 МБ

STATE_FILE = "state/state.json"
TG = f"https://api.telegram.org/bot{BOT_TOKEN}"
UA = "Mozilla/5.0 (X11; Linux x86_64) nizhniy-internet/1.0"

claude = Anthropic(api_key=ANTHROPIC_API_KEY)


# ---------------------------------------------------------------------------
# Состояние
# ---------------------------------------------------------------------------
def load_state() -> dict:
    state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    state.setdefault("offset", 0)
    state.setdefault("seen", [])
    state.setdefault("pending", {})     # id -> кандидат на проверке
    state.setdefault("queue", [])       # одобренные, ждут публикации
    state.setdefault("day", None)
    state.setdefault("posts_today", 0)
    state.setdefault("candidates_today", 0)
    state.setdefault("last_post_ts", 0)
    return state


def save_state(state: dict) -> None:
    os.makedirs("state", exist_ok=True)
    state["seen"] = state["seen"][-1500:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def berlin_now() -> datetime:
    return datetime.now(ZoneInfo("Europe/Berlin"))


def roll_day(state: dict) -> None:
    today = berlin_now().strftime("%Y-%m-%d")
    if state["day"] != today:
        state["day"] = today
        state["posts_today"] = 0
        state["candidates_today"] = 0


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
def tg(method: str, **params):
    r = requests.post(f"{TG}/{method}", json=params, timeout=60)
    data = r.json()
    if not data.get("ok"):
        log.warning(f"Telegram {method}: {data}")
    return data


def keyboard(cid: str) -> dict:
    return {"inline_keyboard": [[
        {"text": "✅ Опубликовать", "callback_data": f"ok:{cid}"},
        {"text": "❌ Пропустить", "callback_data": f"no:{cid}"},
    ]]}


def process_updates(state: dict) -> None:
    data = tg("getUpdates", offset=state["offset"], timeout=0,
              allowed_updates=["callback_query", "message"])
    for upd in data.get("result", []):
        state["offset"] = upd["update_id"] + 1

        msg = upd.get("message")
        if msg and msg.get("chat", {}).get("id") == ADMIN_CHAT_ID:
            text = (msg.get("text") or "").strip()
            if text.startswith("/start") or text.startswith("/status"):
                tg("sendMessage", chat_id=ADMIN_CHAT_ID, text=(
                    f"🔻 Нижний интернет — статус\n"
                    f"На проверке: {len(state['pending'])}\n"
                    f"В очереди на публикацию: {len(state['queue'])}\n"
                    f"Опубликовано сегодня: {state['posts_today']}/{MAX_POSTS_PER_DAY}"))
            continue

        cq = upd.get("callback_query")
        if not cq:
            continue
        if cq.get("from", {}).get("id") != ADMIN_CHAT_ID:
            tg("answerCallbackQuery", callback_query_id=cq["id"], text="Нет доступа")
            continue

        action, _, cid = (cq.get("data") or "").partition(":")
        item = state["pending"].pop(cid, None)
        chat_id = cq["message"]["chat"]["id"]
        message_id = cq["message"]["message_id"]

        if not item:
            tg("answerCallbackQuery", callback_query_id=cq["id"], text="Уже обработано")
            continue
        if action == "ok":
            state["queue"].append(item)
            label = f"✅ В очереди ({len(state['queue'])})"
            log.info(f"Одобрено: {item['title'][:70]}")
        else:
            label = "❌ Пропущено"
            log.info(f"Отклонено: {item['title'][:70]}")
        tg("answerCallbackQuery", callback_query_id=cq["id"], text=label)
        tg("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
           reply_markup={"inline_keyboard": [[{"text": label, "callback_data": "noop:0"}]]})


# ---------------------------------------------------------------------------
# Публикация
# ---------------------------------------------------------------------------
def maybe_publish(state: dict) -> None:
    if not state["queue"]:
        return
    now = berlin_now()
    if not (POST_HOURS[0] <= now.hour < POST_HOURS[1]):
        return
    if state["posts_today"] >= MAX_POSTS_PER_DAY:
        return
    if time.time() - state["last_post_ts"] < MIN_GAP_MINUTES * 60:
        return

    item = state["queue"][0]
    res = tg("sendVideo", chat_id=CHANNEL_ID, video=item["file_id"],
             caption=item["caption"], supports_streaming=True)
    if res.get("ok"):
        state["queue"].pop(0)
        state["posts_today"] += 1
        state["last_post_ts"] = time.time()
        log.info(f"Опубликовано в канал: {item['title'][:70]}")
    else:
        # файл больше недоступен — выкидываем, чтобы не застрять
        state["queue"].pop(0)
        log.error("Не удалось опубликовать, видео убрано из очереди")


# ---------------------------------------------------------------------------
# Reddit
# ---------------------------------------------------------------------------
def fetch_reddit(state: dict) -> list:
    out = []
    for sub in SUBREDDITS:
        try:
            r = requests.get(f"https://www.reddit.com/r/{sub}/top.json",
                             params={"t": "day", "limit": 25},
                             headers={"User-Agent": UA}, timeout=30)
            if r.status_code != 200:
                log.warning(f"r/{sub}: HTTP {r.status_code}")
                continue
            posts = r.json()["data"]["children"]
        except Exception as e:
            log.warning(f"r/{sub}: {e}")
            continue

        for p in posts:
            d = p["data"]
            rv = (d.get("media") or {}).get("reddit_video") or {}
            if not d.get("is_video") or not rv:
                continue
            if d.get("over_18") or d["id"] in state["seen"]:
                continue
            if rv.get("duration", 999) > MAX_DURATION_SEC:
                continue
            out.append({
                "id": d["id"],
                "sub": sub,
                "title": d.get("title", ""),
                "score": d.get("score", 0),
                "url": d.get("url_overridden_by_dest") or f"https://v.redd.it/{d['id']}",
                "permalink": "https://www.reddit.com" + d.get("permalink", ""),
            })
        time.sleep(1)
    out.sort(key=lambda x: x["score"], reverse=True)
    log.info(f"Reddit: найдено {len(out)} новых видео")
    return out[:40]


def pick_and_caption(cands: list, n: int) -> list:
    lines = "\n".join(f"{i}. [r/{c['sub']}, {c['score']}↑] {c['title']}" for i, c in enumerate(cands))
    prompt = f"""Ты редактор русскоязычного Telegram-канала «Нижний интернет» — самые дикие,
странные и безумные видео интернета. Ниже заголовки вирусных видео с Reddit.

Выбери до {n} лучших: неожиданные, абсурдные, «как так вообще», эпичные фейлы,
безумные совпадения, невероятные навыки, странные люди и ситуации.

СТРОГО НЕ БЕРИ: смерть, тяжёлые травмы, кровь, жестокость к животным, насилие над детьми,
сексуальный контент, издевательства над беззащитными людьми, всё, что снято в Германии.

Для каждого выбранного напиши подпись на русском:
- 1-2 короткие строки, цепляющие, с долей сарказма, как пишет живой человек
- можно одно эмодзи
- не выдумывай факты, которых нет в заголовке
- без хэштегов и ссылок

Ответь ТОЛЬКО JSON без пояснений:
{{"picks": [{{"i": 0, "caption": "..."}}]}}

Видео:
{lines}"""
    try:
        resp = claude.messages.create(model="claude-sonnet-5", max_tokens=1200,
                                      messages=[{"role": "user", "content": prompt}])
        raw = "".join(b.text for b in resp.content if hasattr(b, "text"))
        raw = raw[raw.find("{"): raw.rfind("}") + 1]
        picks = json.loads(raw)["picks"]
    except Exception as e:
        log.error(f"Claude не смог выбрать видео: {e}")
        return []
    result = []
    for p in picks:
        i = p.get("i")
        if isinstance(i, int) and 0 <= i < len(cands):
            c = dict(cands[i])
            c["caption"] = (p.get("caption") or "").strip() + "\n\n🔻 Нижний интернет"
            result.append(c)
    return result[:n]


def download(url: str, vid: str):
    os.makedirs("tmp", exist_ok=True)
    out = f"tmp/{vid}.%(ext)s"
    cmd = ["yt-dlp", "-q", "--no-playlist",
           "-f", "bv*[height<=720]+ba/b[height<=720]/bv*+ba/b",
           "--merge-output-format", "mp4",
           "--max-filesize", f"{MAX_FILE_MB}M",
           "-o", out, url]
    try:
        subprocess.run(cmd, check=True, timeout=180)
    except Exception as e:
        log.warning(f"Не удалось скачать {url}: {e}")
        return None
    files = glob.glob(f"tmp/{vid}.*")
    files = [f for f in files if f.endswith(".mp4")]
    if not files or os.path.getsize(files[0]) > MAX_FILE_MB * 1024 * 1024:
        return None
    return files[0]


def expire_pending(state: dict) -> None:
    """Если видео висит без ответа больше суток — убираем, чтобы не забивать очередь."""
    for cid in list(state["pending"]):
        if time.time() - state["pending"][cid].get("ts", 0) > 24 * 3600:
            state["pending"].pop(cid)


def send_for_review(state: dict) -> None:
    expire_pending(state)
    need = MAX_PENDING - len(state["pending"])
    left_today = MAX_CANDIDATES_PER_DAY - state["candidates_today"]
    n = min(need, left_today, 3)
    if n <= 0:
        return

    cands = fetch_reddit(state)
    if not cands:
        return
    picks = pick_and_caption(cands, n)
    for c in cands:  # всё просмотренное помечаем, чтобы не предлагать повторно
        state["seen"].append(c["id"])

    for c in picks:
        path = download(c["url"], c["id"])
        if not path:
            continue
        caption = f"{c['caption']}\n\n— r/{c['sub']} · {c['score']}↑\n{c['permalink']}"
        with open(path, "rb") as f:
            r = requests.post(f"{TG}/sendVideo", data={
                "chat_id": ADMIN_CHAT_ID,
                "caption": caption[:1000],
                "supports_streaming": "true",
                "reply_markup": json.dumps(keyboard(c["id"])),
            }, files={"video": f}, timeout=180).json()
        os.remove(path)
        if not r.get("ok"):
            log.warning(f"Не отправилось на проверку: {r}")
            continue
        c["file_id"] = r["result"]["video"]["file_id"]
        c["ts"] = time.time()
        state["pending"][c["id"]] = c
        state["candidates_today"] += 1
        log.info(f"На проверку: {c['title'][:70]}")


# ---------------------------------------------------------------------------
def main():
    state = load_state()
    roll_day(state)

    process_updates(state)
    save_state(state)

    maybe_publish(state)
    save_state(state)

    send_for_review(state)
    save_state(state)

    log.info(f"Готово. На проверке: {len(state['pending'])}, в очереди: {len(state['queue'])}, "
             f"опубликовано сегодня: {state['posts_today']}")


if __name__ == "__main__":
    main()
