#!/usr/bin/env python3
"""Заливка архива: посты про GTA 6 за последние N дней, по порядку, с паузой."""
import os, re, time, subprocess, logging
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup

import bot

log = logging.getLogger("gta6-backfill")
DAYS = int(os.environ.get("BACKFILL_DAYS", "30"))
MAX_POSTS = int(os.environ.get("BACKFILL_MAX_POSTS", "40"))
PAUSE_SEC = int(os.environ.get("BACKFILL_PAUSE_SEC", "240"))
MAX_PAGES = 25


def parse_page(html, src):
    soup = BeautifulSoup(html, "html.parser")
    out, ids = [], []
    for m in soup.select(".tgme_widget_message"):
        post = m.get("data-post")
        if not post:
            continue
        pid = int(post.split("/")[-1])
        ids.append(pid)
        t = m.select_one(".tgme_widget_message_text")
        tm = m.select_one(".tgme_widget_message_date time")
        if not t or not tm or not tm.get("datetime"):
            continue
        date = datetime.fromisoformat(tm["datetime"])
        for e in t.find_all("tg-emoji"):
            b = e.find("b")
            e.replace_with(b.get_text() if b else "")
        for br in t.find_all("br"):
            br.replace_with("\n")
        text = bot.clean_text(t.get_text())
        if len(text) < 40:
            continue
        image = None
        ph = m.select_one(".tgme_widget_message_photo_wrap")
        if ph:
            mm = re.search(r"url\('([^']+)'\)", ph.get("style", ""))
            if mm:
                image = mm.group(1)
        out.append({"source": src["name"], "link": f"https://t.me/{post}", "text": text[:1500],
                    "image": image, "date": date, "hash": bot.content_hash(text)})
    return out, ids


def collect(src, cutoff):
    items, before = [], None
    for _ in range(MAX_PAGES):
        url = f"https://t.me/s/{src['channel']}" + (f"?before={before}" if before else "")
        r = requests.get(url, headers=bot.UA, timeout=30)
        r.raise_for_status()
        page, ids = parse_page(r.text, src)
        if not ids:
            break
        items += page
        before = min(ids)
        oldest = min((i["date"] for i in page), default=None)
        if oldest and oldest < cutoff:
            break
        time.sleep(1)
    return [i for i in items if i["date"] >= cutoff]


def git_push_state(msg):
    sh = lambda *a: subprocess.run(a, check=False, capture_output=True, text=True)
    sh("git", "config", "user.name", "github-actions")
    sh("git", "config", "user.email", "actions@github.com")
    sh("git", "add", bot.STATE_FILE)
    if sh("git", "diff", "--cached", "--quiet").returncode == 0:
        return
    sh("git", "commit", "-m", msg)
    for _ in range(3):
        if sh("git", "push").returncode == 0:
            return
        sh("git", "pull", "--rebase", "-X", "theirs")


def main():
    cutoff = datetime.now(timezone.utc) - timedelta(days=DAYS)
    state = bot.load_state()
    # блокируем обычный почасовой бот на время архива
    state["next_post_not_before"] = time.time() + (MAX_POSTS * (PAUSE_SEC + 30) + 600)
    bot.save_state(state)
    git_push_state("Backfill: pause regular bot")

    items, seen = [], set(state["seen_hashes"])
    for src in bot.SOURCES:
        try:
            got = collect(src, cutoff)
        except Exception as e:
            log.warning(f"{src['name']}: {e}")
            continue
        log.info(f"{src['name']}: {len(got)} постов за {DAYS} дн.")
        items += got
    items = [i for i in items if i["link"] not in state["seen_links"]
             and any(k in i["text"].lower() for k in bot.KEYWORDS)]
    items.sort(key=lambda i: i["date"])
    uniq = []
    for i in items:
        if i["hash"] in seen:
            continue
        seen.add(i["hash"])
        uniq.append(i)
    log.info(f"К публикации подходит: {len(uniq)}, за этот запуск максимум {MAX_POSTS}")

    posted = 0
    for it in uniq:
        if posted >= MAX_POSTS:
            break
        body = bot.rewrite(it["text"], it["source"])
        state["seen_links"].append(it["link"]); state["seen_hashes"].append(it["hash"])
        if not body:
            continue
        if bot.send_to_telegram(bot.build_final_text(body), it["image"]):
            posted += 1
            log.info(f"Опубликован {posted}: {it['date'].date()} {it['text'][:50]}")
            bot.save_state(state)
            git_push_state(f"Backfill {posted}")
            time.sleep(PAUSE_SEC)
        else:
            state["seen_links"].pop(); state["seen_hashes"].pop()
            log.error("Не удалось отправить, останавливаюсь")
            break
    state["next_post_not_before"] = None
    bot.save_state(state)
    git_push_state("Backfill done")
    log.info(f"Готово, опубликовано: {posted}, осталось в архиве: {max(0, len(uniq) - posted)}")


if __name__ == "__main__":
    main()
