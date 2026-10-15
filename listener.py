"""Слушает канал-источник через второй аккаунт Telegram и запускает публикацию сразу при новом посте.
Раз в POLL секунд дополнительно запускает ботов на случай, если пост был отложен паузой между публикациями."""
import os, sys, time, asyncio, subprocess, logging

from telethon import TelegramClient, events
from telethon.sessions import StringSession

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("listener")

CHANNEL = os.environ.get("SOURCE_CHANNEL", "gta6deluxe")
RUN_SECONDS = int(os.environ.get("LISTENER_SECONDS", str(5 * 3600 + 40 * 60)))
POLL = int(os.environ.get("LISTENER_POLL", "900"))
DEBOUNCE = 25


def sh(*a, **kw):
    return subprocess.run(a, capture_output=True, text=True, **kw)


def run_bots():
    sh("git", "pull", "--rebase", "--autostash")
    for script, state_file in (("bot.py", "state/seen.json"), ("threads_bot.py", "state/threads_seen.json")):
        env = dict(os.environ, STATE_FILE=state_file, THREADS_STATE_FILE="state/threads_seen.json")
        env.pop("THREADS_FORCE_LATEST", None)
        try:
            r = subprocess.run([sys.executable, script], capture_output=True, text=True, timeout=1500, env=env)
            tail = (r.stdout + r.stderr).strip().splitlines()[-6:]
            log.info(f"{script}: код {r.returncode}\n" + "\n".join(tail))
        except Exception as e:
            log.warning(f"{script}: {e}")
    sh("git", "add", "state")
    if sh("git", "diff", "--cached", "--quiet").returncode != 0:
        sh("git", "commit", "-m", "Update state (listener)")
        for _ in range(3):
            if sh("git", "push").returncode == 0:
                break
            sh("git", "pull", "--rebase", "--autostash")


async def main():
    started = time.time()
    client = TelegramClient(StringSession(os.environ["TG_SESSION"].strip()), int(os.environ["TG_API_ID"].strip()), os.environ["TG_API_HASH"].strip())
    await client.start()
    log.info(f"Слушаю канал {CHANNEL}")
    trigger = asyncio.Event()

    @client.on(events.NewMessage(chats=CHANNEL))
    async def on_new(event):
        log.info("Новый пост в источнике")
        trigger.set()

    loop = asyncio.get_running_loop()
    trigger.set()  # первый проход сразу при старте
    while time.time() - started < RUN_SECONDS:
        try:
            await asyncio.wait_for(trigger.wait(), timeout=POLL)
            await asyncio.sleep(DEBOUNCE)  # дать посту дойти до t.me/s и собрать альбом
        except asyncio.TimeoutError:
            pass
        trigger.clear()
        await client.disconnect()  # одна и та же сессия не должна работать из двух процессов одновременно
        try:
            await loop.run_in_executor(None, run_bots)
        finally:
            await client.connect()
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
