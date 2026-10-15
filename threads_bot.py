#!/usr/bin/env python3
"""GTA 6 Threads Bot: те же источники, что у Telegram-бота -> Claude -> Threads API."""
import os, sys, time, random, json, subprocess, logging
from datetime import datetime, timezone

import requests

import bot  # переиспользуем парсинг каналов, фильтр, скачивание, ffmpeg и уведомления

log = logging.getLogger("gta6-threads")
THREADS_API = "https://graph.threads.net/v1.0"
TOKEN = os.environ["THREADS_ACCESS_TOKEN"]
USER_ID = os.environ.get("THREADS_USER_ID") or "me"
STATE_FILE = os.environ.get("THREADS_STATE_FILE", "state/threads_seen.json")
MIN_INTERVAL = int(os.environ.get("THREADS_MIN_INTERVAL", "90"))
MAX_INTERVAL = int(os.environ.get("THREADS_MAX_INTERVAL", "180"))
MAX_VIDEO_SECONDS = int(os.environ.get("THREADS_MAX_VIDEO_SECONDS", "90"))
PROFILE_NOTE = "Больше — в шапке профиля."
REPO = os.environ.get("GITHUB_REPOSITORY", "monsterren1312/nizhniy-internet")
MEDIA_DIR = "media"


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


def sh(*a):
    return subprocess.run(a, check=False, capture_output=True, text=True)


def git_push(msg, *paths):
    sh("git", "config", "user.name", "github-actions")
    sh("git", "config", "user.email", "actions@github.com")
    sh("git", "add", "-A", *paths)
    if sh("git", "diff", "--cached", "--quiet").returncode == 0:
        return True
    sh("git", "commit", "-m", msg)
    for _ in range(4):
        if sh("git", "push").returncode == 0:
            return True
        sh("git", "pull", "--rebase", "-X", "theirs")
        time.sleep(2)
    return False


def rewrite_for_threads(text, source):
    prompt = f"""Ты ведёшь Threads-аккаунт про GTA 6 на русском языке и пишешь для вовлечения.

Исходный пост из канала «{source}»:

{text}

Напиши пост для Threads строго по структуре:
1. Первая строка: короткий цепляющий заголовок, 3-7 слов, вопрос или резкое утверждение. Без кавычек и Markdown.
2. Пустая строка.
3. 2-3 коротких абзаца, спокойным тоном, только факты из поста, без выдумок и без лишних прилагательных.
4. Если это слух, утечка или инсайд, прямо напиши, что это неподтверждено. Заголовок не должен подавать слух как факт.
5. Без хэштегов, ссылок, названия источника и призывов подписаться.
6. Максимум 380 символов всего.
7. Если в посте нет новости про GTA 6 или Rockstar (реклама, опрос, мем), ответь одним словом: SKIP.

Ответь только готовым текстом."""
    try:
        r = bot.client.messages.create(model="claude-sonnet-4-6", max_tokens=400,
                                       messages=[{"role": "user", "content": prompt}])
        t = "".join(b.text for b in r.content if hasattr(b, "text")).strip()
        return None if not t or t.upper().startswith("SKIP") else t
    except Exception as e:
        log.error(f"Anthropic: {e}")
        return None


def api(method, path, **kw):
    r = requests.request(method, f"{THREADS_API}/{path}", timeout=60, **kw)
    try:
        data = r.json()
    except Exception:
        data = {}
    return r.ok, data, r.text[:300]


def wait_ready(cid, tries=40):
    for _ in range(tries):
        ok, data, raw = api("GET", cid, params={"fields": "status,error_message", "access_token": TOKEN})
        st = data.get("status")
        if st == "FINISHED":
            return True
        if st in ("ERROR", "EXPIRED"):
            log.warning(f"Контейнер {st}: {data.get('error_message')}")
            return False
        time.sleep(8)
    return False


def publish(text, media_type="TEXT", url=None):
    params = {"media_type": media_type, "text": text, "access_token": TOKEN}
    if media_type == "IMAGE":
        params["image_url"] = url
    elif media_type == "VIDEO":
        params["video_url"] = url
    ok, data, raw = api("POST", f"{USER_ID}/threads", data=params)
    if not ok or not data.get("id"):
        log.warning(f"Не создан контейнер ({media_type}): {raw}")
        return False
    cid = data["id"]
    if media_type != "TEXT" and not wait_ready(cid):
        return False
    time.sleep(3)
    ok, data, raw = api("POST", f"{USER_ID}/threads_publish",
                        data={"creation_id": cid, "access_token": TOKEN})
    if not ok or not data.get("id"):
        log.warning(f"Не опубликован: {raw}")
        return False
    return True


def host_video(video_url, pid):
    """Скачивает webm, конвертирует в mp4, коммитит в репозиторий и отдаёт публичную ссылку."""
    if not bot.download(video_url, "/tmp/th_in") or not bot.to_mp4("/tmp/th_in", "/tmp/th_out.mp4"):
        return None
    os.makedirs(MEDIA_DIR, exist_ok=True)
    name = f"{MEDIA_DIR}/{pid}.mp4"
    with open("/tmp/th_out.mp4", "rb") as src, open(name, "wb") as dst:
        dst.write(src.read())
    if not git_push(f"media {pid}", name):
        return None
    return f"https://raw.githubusercontent.com/{REPO}/main/{name}"


def cleanup_media():
    if os.path.isdir(MEDIA_DIR) and os.listdir(MEDIA_DIR):
        sh("git", "rm", "-rq", "--ignore-unmatch", MEDIA_DIR)
        git_push("cleanup media")


def main():
    log.info("Запуск GTA 6 Threads бота")
    state = load_state()
    nb = state.get("next_post_not_before")
    fv = os.environ.get("THREADS_FORCE_LATEST", "")
    force_n = int(fv) if fv.isdigit() else 0
    if nb and not (fv == "true" or force_n) and datetime.now(timezone.utc).timestamp() < nb:
        log.info("Ещё не время для следующего поста")
        return
    cleanup_media()
    force = fv == "true" or force_n > 0
    if force:
        allc = bot.fetch_candidates({"seen_links": [], "seen_hashes": [], "recent_texts": []})
        allc = [c for c in allc if c["link"] not in state["seen_links"]]
        cands = allc[:force_n] if force_n else allc[:1]
        state["initialized"] = True
    else:
        cands = bot.fetch_candidates(state)  # использует seen_* из нашего состояния
    log.info(f"Кандидатов: {len(cands)}")
    if not state.get("initialized"):
        # первый запуск: всё, что уже есть в каналах-источниках, считаем старым и не постим
        for it in cands:
            state["seen_links"].append(it["link"]); state["seen_hashes"].append(it["hash"])
        state["initialized"] = True
        save_state(state)
        git_push("Threads: skip existing posts", STATE_FILE)
        log.info(f"Пропущено старых постов: {len(cands)}. Дальше публикуются только новые.")
        return
    for it in cands[:5]:
        body = rewrite_for_threads(it["text"], it["source"])
        state["seen_links"].append(it["link"]); state["seen_hashes"].append(it["hash"])
        if not body:
            continue
        info = it.get("info") or {}
        has_video = info.get("has_video")
        long_video = (info.get("duration") or 0) > MAX_VIDEO_SECONDS
        text = f"{body}\n\n{PROFILE_NOTE}"[:500]
        text_tg = f"{body}\n\nВидео смотрите в нашем Telegram-канале @newnewsgta6. {PROFILE_NOTE}"[:500]
        ok = False
        video_missing = bool(has_video)
        if it.get("video") and not long_video:
            url = host_video(it["video"], it["link"].rsplit("/", 1)[-1])
            if url and publish(text, "VIDEO", url):
                ok, video_missing = True, False
        fb_text = text_tg if video_missing else text
        if not ok and it.get("image"):
            ok = publish(fb_text, "IMAGE", it["image"])
        if not ok:
            ok = publish(fb_text, "TEXT")
        if ok:
            if video_missing:
                text_for_owner = fb_text + "\n\n(Threads: видео не прикреплено, в тексте отсылка на Telegram)"
                bot.notify_owner(it, text_for_owner)
            delay = random.uniform(MIN_INTERVAL, MAX_INTERVAL)
            state["next_post_not_before"] = datetime.now(timezone.utc).timestamp() + delay * 60
            log.info(f"Опубликовано. Следующий пост через {delay:.0f} мин")
            if not force_n:
                break
            time.sleep(60)
            continue
        log.error("Публикация не удалась, пробую следующий пост")
    save_state(state)
    git_push("Update threads state", STATE_FILE)


if __name__ == "__main__":
    main()
