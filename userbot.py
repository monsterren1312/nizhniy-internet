"""Публикация медиа через второй аккаунт Telegram (MTProto, Telethon).
Берёт медиа прямо из поста канала-источника (в т.ч. альбом целиком, без скачивания) и кладёт в наш канал с новой подписью."""
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


def post_video(link, caption, target):
    """Публикует медиа поста-источника (в т.ч. альбом целиком) с нашей подписью.
    Возвращает id первого сообщения в нашем канале или None."""
    m = re.search(r"t\.me/([^/]+)/(\d+)", link or "")
    if not (enabled() and m):
        return None
    try:
        from telethon.sync import TelegramClient
        from telethon.sessions import StringSession
        from telethon.tl.types import MessageMediaWebPage
        chan, pid = m.group(1), int(m.group(2))
        with TelegramClient(StringSession(SESSION), int(API_ID), API_HASH) as client:
            msg = client.get_messages(chan, ids=pid)
            if not msg or not msg.media or isinstance(msg.media, MessageMediaWebPage):
                log.warning(f"В посте {link} нет медиа")
                return None
            media = [msg.media]
            gid = getattr(msg, "grouped_id", None)
            if gid:
                ids = [i for i in range(pid - 10, pid + 11) if i >= 1]
                group = [x for x in client.get_messages(chan, ids=ids)
                         if x and x.media and getattr(x, "grouped_id", None) == gid]
                group.sort(key=lambda x: x.id)
                if group:
                    media = [x.media for x in group]
            dest = int(target) if re.fullmatch(r"-?\d+", str(target)) else target
            long_text = len(caption) > 1024
            cap = None if long_text else to_md(caption)
            sent = client.send_file(dest, media if len(media) > 1 else media[0],
                                    caption=cap, parse_mode="md")
            first = sent[0] if isinstance(sent, (list, tuple)) else sent
            if long_text:
                client.send_message(dest, to_md(caption), parse_mode="md", link_preview=False)
            return first.id
    except Exception as e:
        log.warning(f"Userbot не смог опубликовать {link}: {e}")
        return None
