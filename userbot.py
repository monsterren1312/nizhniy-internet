"""Публикация длинных видео через второй аккаунт Telegram (MTProto, Telethon).
Берёт медиа прямо из поста канала-источника (без скачивания) и кладёт в наш канал с новой подписью."""
import os, re, logging

log = logging.getLogger("gta6-userbot")

API_ID = os.environ.get("TG_API_ID", "").strip()
API_HASH = os.environ.get("TG_API_HASH", "").strip()
SESSION = os.environ.get("TG_SESSION", "").strip()


def enabled():
    return bool(API_ID and API_HASH and SESSION)


def to_md(text):
    """Bot API Markdown (*жирный*) -> Markdown Telethon (**жирный**)."""
    return re.sub(r"(?<!\*)\*(?!\*)([^*\n]+)\*(?!\*)", r"**\1**", text)


def cut(text, limit=1024):
    if len(text) <= limit:
        return text
    at = text.rfind("\n", 0, limit - 1)
    return text[: at if at > 200 else limit - 1].rstrip() + "…"


def post_video(link, caption, target):
    """Возвращает id сообщения в нашем канале или None."""
    m = re.search(r"t\.me/([^/]+)/(\d+)", link or "")
    if not (enabled() and m):
        return None
    try:
        from telethon.sync import TelegramClient
        from telethon.sessions import StringSession
        with TelegramClient(StringSession(SESSION), int(API_ID), API_HASH) as client:
            msg = client.get_messages(m.group(1), ids=int(m.group(2)))
            if not msg or not msg.media:
                log.warning(f"В посте {link} нет медиа")
                return None
            dest = int(target) if re.fullmatch(r"-?\d+", str(target)) else target
            sent = client.send_file(dest, msg.media, caption=to_md(cut(caption)), parse_mode="md")
            return sent.id
    except Exception as e:
        log.warning(f"Userbot не смог опубликовать {link}: {e}")
        return None
