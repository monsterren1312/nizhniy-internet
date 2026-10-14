#!/usr/bin/env python3
"""Бот-приёмник: владелец пересылает боту пост с видео (любого размера),
бот пишет новый текст через Claude и публикует в канал без чужой подписи."""
import os, json, time, logging

import requests

import bot  # rewrite, build_final_text, OWNER_IDS, TELEGRAM_*

log = logging.getLogger("gta6-inbox")
API = f"https://api.telegram.org/bot{bot.TELEGRAM_BOT_TOKEN}"
RUN_SECONDS = int(os.environ.get("INBOX_RUN_SECONDS", "280"))
MEDIA_KEYS = ("video", "photo", "animation", "document", "video_note")


def call(method, **data):
    try:
        r = requests.post(f"{API}/{method}", data=data, timeout=120)
        return r.json()
    except Exception as e:
        log.warning(f"{method}: {e}")
        return {"ok": False, "description": str(e)}


def reply(chat_id, text):
    call("sendMessage", chat_id=chat_id, text=text[:4000], disable_web_page_preview="true")


def cut(text, limit=1024):
    if len(text) <= limit:
        return text
    cut_at = text.rfind("\n", 0, limit - 1)
    return text[: cut_at if cut_at > 200 else limit - 1].rstrip() + "…"


def publish(msg, final, has_media):
    if has_media:
        caption = cut(final)
        for parse in ("Markdown", None):
            d = dict(chat_id=bot.TELEGRAM_CHAT_ID, from_chat_id=msg["chat"]["id"],
                     message_id=msg["message_id"], caption=caption)
            if parse:
                d["parse_mode"] = parse
            res = call("copyMessage", **d)
            if res.get("ok"):
                return res["result"]["message_id"]
            log.warning(f"copyMessage ({parse}): {res.get('description')}")
        return None
    for parse in ("Markdown", None):
        d = dict(chat_id=bot.TELEGRAM_CHAT_ID, text=final, disable_web_page_preview="true")
        if parse:
            d["parse_mode"] = parse
        res = call("sendMessage", **d)
        if res.get("ok"):
            return res["result"]["message_id"]
    return None


def handle(msg):
    uid = str(msg.get("from", {}).get("id", ""))
    if uid not in bot.OWNER_IDS:
        return
    chat_id = msg["chat"]["id"]
    text = (msg.get("caption") or msg.get("text") or "").strip()
    if text.startswith("/"):
        reply(chat_id, "Перешлите мне пост с видео из канала-источника. Я напишу новый текст "
                       "и опубликую его в канале вместе с видео.")
        return
    has_media = any(k in msg for k in MEDIA_KEYS)
    if not has_media and not text:
        return
    if not text:
        reply(chat_id, "В этом сообщении нет текста, на основе которого можно написать пост.")
        return
    origin = msg.get("forward_origin") or {}
    source = (origin.get("chat") or {}).get("title") or origin.get("sender_user_name") or "источник"
    body = bot.rewrite(text, source)
    if not body:
        reply(chat_id, "Claude решил, что это не новость про GTA 6, и пропустил пост.")
        return
    final = bot.build_final_text(body)
    mid = publish(msg, final, has_media)
    if mid:
        bot.FIRST_POST_ID = mid
        reply(chat_id, "✅ Опубликовано: " + (bot.post_url() or "готово"))
    else:
        reply(chat_id, "Не удалось опубликовать пост. Подробности в логе запуска.")


def main():
    log.info("Запуск приёмника пересланных постов")
    deadline = time.time() + RUN_SECONDS
    offset = None
    while time.time() < deadline:
        params = {"timeout": 25, "allowed_updates": json.dumps(["message"])}
        if offset:
            params["offset"] = offset
        try:
            r = requests.get(f"{API}/getUpdates", params=params, timeout=40).json()
        except Exception as e:
            log.warning(f"getUpdates: {e}")
            time.sleep(3)
            continue
        if not r.get("ok"):
            log.error(f"getUpdates: {r}")
            time.sleep(5)
            continue
        for u in r.get("result", []):
            offset = u["update_id"] + 1
            if "message" in u:
                try:
                    handle(u["message"])
                except Exception as e:
                    log.exception(f"Ошибка обработки: {e}")
    if offset:  # подтверждаем обработанные обновления
        requests.get(f"{API}/getUpdates", params={"offset": offset, "timeout": 0}, timeout=30)
    log.info("Готово")


if __name__ == "__main__":
    main()
