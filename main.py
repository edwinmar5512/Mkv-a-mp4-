import os
import time
import logging
import asyncio
import subprocess
import aiohttp
from aiohttp import web
from pyrogram import Client, filters
from pyrogram.types import Message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("mkv_bot")

# ---------- Config (variables de entorno en Render) ----------
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
PORT = int(os.environ.get("PORT", 10000))

DOWNLOAD_DIR = "/app/downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

app = Client(
    "mkv_bot_render",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)


# ---------- DEBUG: loguear absolutamente todo lo que llega ----------
@app.on_message(filters.all, group=-1)
async def debug_all(client, message: Message):
    logger.info(
        f"UPDATE RECIBIDO -> from={message.from_user.id if message.from_user else '?'} "
        f"chat={message.chat.id} text={message.text!r} doc={bool(message.document)}"
    )


# ---------- Utilidades de progreso ----------
def humanbytes(size: float) -> str:
    if not size:
        return "0B"
    power = 1024
    n = 0
    units = ["B", "KB", "MB", "GB", "TB"]
    while size > power and n < len(units) - 1:
        size /= power
        n += 1
    return f"{size:.2f}{units[n]}"


async def progress_bar(current, total, message: Message, prefix: str, start_time: float):
    now = time.time()
    diff = now - start_time
    if diff < 1 and current != total:
        return
    percentage = current * 100 / total
    speed = current / diff if diff > 0 else 0
    bar_len = 20
    filled = int(bar_len * percentage / 100)
    bar = "█" * filled + "░" * (bar_len - filled)

    text = (
        f"{prefix}\n"
        f"[{bar}] {percentage:.1f}%\n"
        f"{humanbytes(current)} / {humanbytes(total)}\n"
        f"Velocidad: {humanbytes(speed)}/s"
    )

    try:
        last = getattr(progress_bar, f"_last_{message.id}", 0)
        if now - last > 2 or current == total:
            await message.edit_text(text)
            setattr(progress_bar, f"_last_{message.id}", now)
    except Exception:
        pass


def get_duration_seconds(filepath: str) -> float:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", filepath,
            ],
            capture_output=True, text=True,
        )
        return float(result.stdout.strip())
    except Exception:
        return 0.0


async def convert_with_progress(input_path: str, output_path: str, status_msg: Message):
    duration = get_duration_seconds(input_path)

    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        output_path,
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    last_update = 0.0
    while True:
        line = await process.stderr.readline()
        if not line:
            break
        line = line.decode("utf-8", errors="ignore")
        if "time=" in line and duration > 0:
            try:
                time_str = line.split("time=")[1].split(" ")[0]
                h, m, s = time_str.split(":")
                current_seconds = int(h) * 3600 + int(m) * 60 + float(s)
                percentage = min(current_seconds / duration * 100, 100)
                now = time.time()
                if now - last_update > 3:
                    bar_len = 20
                    filled = int(bar_len * percentage / 100)
                    bar = "█" * filled + "░" * (bar_len - filled)
                    await status_msg.edit_text(
                        f"🔄 Convirtiendo a MP4...\n[{bar}] {percentage:.1f}%"
                    )
                    last_update = now
            except Exception:
                pass

    await process.wait()
    return process.returncode == 0


# ---------- Handlers ----------
@app.on_message(filters.command("start"))
async def start_handler(client, message: Message):
    logger.info("Handler /start disparado")
    await message.reply_text(
        "¡Hola! Mandame un archivo .mkv y te lo devuelvo convertido a .mp4."
    )


@app.on_message(filters.document)
async def handle_document(client, message: Message):
    doc = message.document
    filename = doc.file_name or ""

    if not filename.lower().endswith(".mkv"):
        await message.reply_text("Solo acepto archivos .mkv por ahora.")
        return

    status_msg = await message.reply_text("⬇️ Descargando...")
    start_time = time.time()

    input_path = os.path.join(DOWNLOAD_DIR, filename)
    output_path = os.path.join(DOWNLOAD_DIR, filename.rsplit(".", 1)[0] + ".mp4")

    try:
        await message.download(
            file_name=input_path,
            progress=progress_bar,
            progress_args=(status_msg, "⬇️ Descargando...", start_time),
        )

        await status_msg.edit_text("🔄 Convirtiendo a MP4...")
        ok = await convert_with_progress(input_path, output_path, status_msg)

        if not ok:
            await status_msg.edit_text("❌ Error al convertir el archivo.")
            return

        await status_msg.edit_text("⬆️ Subiendo...")
        upload_start = time.time()
        await client.send_video(
            chat_id=message.chat.id,
            video=output_path,
            caption=filename.rsplit(".", 1)[0] + ".mp4",
            progress=progress_bar,
            progress_args=(status_msg, "⬆️ Subiendo...", upload_start),
        )
        await status_msg.delete()

    except Exception as e:
        logger.exception("Error procesando documento")
        await status_msg.edit_text(f"❌ Error: {e}")

    finally:
        for f in (input_path, output_path):
            if os.path.exists(f):
                os.remove(f)


# ---------- Servidor HTTP (health check + debug) ----------
async def health(request):
    return web.Response(text="OK")


async def webhook_info(request):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getWebhookInfo"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
    return web.json_response(data)


async def getme_info(request):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getMe"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
    return web.json_response(data)


async def start_web_server():
    web_app = web.Application()
    web_app.router.add_get("/", health)
    web_app.router.add_get("/debug/webhook", webhook_info)
    web_app.router.add_get("/debug/getme", getme_info)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()


async def main():
    await start_web_server()
    logger.info("Servidor web arrancado, iniciando cliente de Pyrogram...")
    await app.start()
    me = await app.get_me()
    logger.info(f"Bot corriendo... conectado como @{me.username} (id={me.id})")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
