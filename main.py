import asyncio
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import sys
import time
import zipfile
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import FSInputFile, Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ============================================================
# MUSKAN HOSTING - FREE TELEGRAM BOT HOSTING PANEL
# ============================================================
# Required environment variables:
#   BOT_TOKEN = your Telegram bot token
#   ADMIN_ID  = your Telegram numeric admin ID
#
# Optional:
#   HOST_ROOT = directory for hosted bots (default: ./hosted_bots)
#   MAX_BOTS_PER_USER = 2
#   MAX_ZIP_MB = 5
#   MAX_LOG_LINES = 80
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
HOST_ROOT = Path(os.getenv("HOST_ROOT", str(BASE_DIR / "hosted_bots"))).resolve()
DB_PATH = BASE_DIR / "hosting.db"
MAX_BOTS_PER_USER = int(os.getenv("MAX_BOTS_PER_USER", "2"))
MAX_ZIP_MB = int(os.getenv("MAX_ZIP_MB", "5"))
MAX_LOG_LINES = int(os.getenv("MAX_LOG_LINES", "80"))

BOT_TOKEN = os.getenv("8406705149:AAH-_QJeqAgo6iKj6z1mT60twSVjf3Rn6rc", "").strip()
ADMIN_ID_RAW = os.getenv("7940269685", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is missing.")
if not ADMIN_ID_RAW.isdigit():
    raise RuntimeError("ADMIN_ID environment variable must be a numeric Telegram ID.")
ADMIN_ID = int(ADMIN_ID_RAW)

HOST_ROOT.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("muskan_hosting")

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

DB_LOCK = asyncio.Lock()
PROCESS_LOCK = asyncio.Lock()
processes: dict[int, asyncio.subprocess.Process] = {}
stop_requested: set[int] = set()


def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db_connect()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT DEFAULT '',
        first_name TEXT DEFAULT '',
        blocked INTEGER DEFAULT 0,
        created_at INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS bots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        folder TEXT NOT NULL UNIQUE,
        entry_file TEXT NOT NULL,
        status TEXT DEFAULT 'stopped',
        created_at INTEGER NOT NULL,
        last_started INTEGER DEFAULT 0,
        FOREIGN KEY(user_id) REFERENCES users(user_id)
    );
    """)
    conn.commit()
    conn.close()


async def db_execute(query, params=(), fetch=False, one=False):
    async with DB_LOCK:
        conn = db_connect()
        cur = conn.execute(query, params)
        rows = cur.fetchone() if one else (cur.fetchall() if fetch else None)
        conn.commit()
        conn.close()
        return rows


def safe_name(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("._-")
    return value[:40] or "bot"


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


async def ensure_user(user):
    await db_execute(
        """INSERT INTO users(user_id, username, first_name, created_at)
           VALUES(?,?,?,?)
           ON CONFLICT(user_id) DO UPDATE SET
             username=excluded.username,
             first_name=excluded.first_name""",
        (user.id, user.username or "", user.first_name or "", int(time.time()))
    )


async def is_blocked(user_id: int) -> bool:
    row = await db_execute(
        "SELECT blocked FROM users WHERE user_id=?",
        (user_id,), one=True
    )
    return bool(row and row["blocked"])


async def guard(message: Message) -> bool:
    await ensure_user(message.from_user)
    if await is_blocked(message.from_user.id):
        await message.answer("🚫 <b>Your account is blocked.</b>")
        return False
    return True


def main_keyboard(user_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Add Bot", callback_data="add_bot")
    kb.button(text="🤖 My Bots", callback_data="my_bots")
    kb.button(text="📖 How To Use", callback_data="howto")
    kb.button(text="💬 Support", callback_data="support")
    kb.adjust(2)
    if is_admin(user_id):
        kb.button(text="👑 Admin Panel", callback_data="admin")
        kb.adjust(2)
    return kb.as_markup()


def back_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="🔙 Main Menu", callback_data="home")
    return kb.as_markup()


def admin_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Dashboard", callback_data="admin_dashboard")
    kb.button(text="👥 Users", callback_data="admin_users")
    kb.button(text="🤖 All Bots", callback_data="admin_bots")
    kb.button(text="🟢 Running", callback_data="admin_running")
    kb.button(text="🔴 Stopped", callback_data="admin_stopped")
    kb.button(text="🔙 Main Menu", callback_data="home")
    kb.adjust(2)
    return kb.as_markup()


async def bot_count(user_id: int) -> int:
    row = await db_execute(
        "SELECT COUNT(*) AS c FROM bots WHERE user_id=?",
        (user_id,), one=True
    )
    return int(row["c"])


async def get_user_bots(user_id: int):
    return await db_execute(
        "SELECT * FROM bots WHERE user_id=? ORDER BY id DESC",
        (user_id,), fetch=True
    )


async def get_bot(bot_id: int):
    return await db_execute(
        "SELECT * FROM bots WHERE id=?",
        (bot_id,), one=True
    )


def bot_action_keyboard(bot_id: int, status: str):
    kb = InlineKeyboardBuilder()
    if status == "running":
        kb.button(text="⏹ Stop", callback_data=f"stop:{bot_id}")
        kb.button(text="🔄 Restart", callback_data=f"restart:{bot_id}")
    else:
        kb.button(text="▶️ Start", callback_data=f"start:{bot_id}")
    kb.button(text="📜 Logs", callback_data=f"logs:{bot_id}")
    kb.button(text="🗑 Delete", callback_data=f"delete:{bot_id}")
    kb.button(text="🔙 My Bots", callback_data="my_bots")
    kb.adjust(2)
    return kb.as_markup()


async def read_logs(bot_id: int) -> str:
    folder = HOST_ROOT / str(bot_id)
    log_file = folder / "bot.log"
    if not log_file.exists():
        return "No log file yet."
    try:
        text = log_file.read_text(errors="replace")
        lines = text.splitlines()[-MAX_LOG_LINES:]
        return "\n".join(lines) or "No logs yet."
    except Exception as e:
        return f"Could not read logs: {e}"


async def detect_entry(folder: Path) -> Optional[str]:
    preferred = ["main.py", "bot.py", "app.py", "run.py"]
    for name in preferred:
        if (folder / name).is_file():
            return name
    py_files = sorted(folder.glob("*.py"))
    return py_files[0].name if py_files else None


def valid_zip_members(zf: zipfile.ZipFile) -> bool:
    # Prevent zip-slip/path traversal.
    root = Path(HOST_ROOT).resolve()
    for info in zf.infolist():
        if info.is_dir():
            continue
        target = (root / info.filename).resolve()
        if root not in target.parents and target != root:
            return False
        if info.file_size > 3 * 1024 * 1024:
            return False
    return True


async def install_requirements(folder: Path) -> tuple[bool, str]:
    req = folder / "requirements.txt"
    if not req.exists():
        return True, "No requirements.txt found."

    log = folder / "bot.log"
    with log.open("a", encoding="utf-8") as f:
        f.write("\n=== Installing requirements ===\n")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
            "--no-input", "-r", str(req),
            cwd=str(folder),
            stdout=f,
            stderr=asyncio.subprocess.STDOUT
        )
        try:
            code = await asyncio.wait_for(proc.wait(), timeout=180)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return False, "requirements.txt installation timed out."
    if code != 0:
        return False, f"pip exited with code {code}."
    return True, "Requirements installed."


async def stream_process(bot_id: int, proc: asyncio.subprocess.Process, log_file: Path):
    with log_file.open("a", encoding="utf-8") as f:
        f.write(f"\n=== Process started PID={proc.pid} ===\n")
        while True:
            data = await proc.stdout.readline()
            if not data:
                break
            line = data.decode("utf-8", errors="replace")
            f.write(line)
            f.flush()
        code = await proc.wait()
        f.write(f"=== Process exited code={code} ===\n")


async def start_hosted_bot(bot_id: int, auto_restart=True):
    async with PROCESS_LOCK:
        if bot_id in processes and processes[bot_id].returncode is None:
            return True, "Already running."

        row = await get_bot(bot_id)
        if not row:
            return False, "Bot not found."

        folder = HOST_ROOT / str(bot_id)
        entry = folder / row["entry_file"]
        if not entry.exists():
            return False, "Entry file is missing."

        ok, msg = await install_requirements(folder)
        if not ok:
            await db_execute("UPDATE bots SET status='error' WHERE id=?", (bot_id,))
            return False, msg

        log_file = folder / "bot.log"
        stop_requested.discard(bot_id)

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-u", row["entry_file"],
                cwd=str(folder),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True
            )
        except Exception as e:
            await db_execute("UPDATE bots SET status='error' WHERE id=?", (bot_id,))
            return False, f"Start failed: {e}"

        processes[bot_id] = proc
        await db_execute(
            "UPDATE bots SET status='running', last_started=? WHERE id=?",
            (int(time.time()), bot_id)
        )

        asyncio.create_task(stream_process(bot_id, proc, log_file))
        asyncio.create_task(watch_process(bot_id, proc, auto_restart))
        return True, f"Started (PID {proc.pid})."


async def watch_process(bot_id: int, proc: asyncio.subprocess.Process, auto_restart=True):
    try:
        code = await proc.wait()
    except Exception:
        code = -1

    async with PROCESS_LOCK:
        current = processes.get(bot_id)
        if current is proc:
            processes.pop(bot_id, None)

    if bot_id in stop_requested:
        stop_requested.discard(bot_id)
        await db_execute("UPDATE bots SET status='stopped' WHERE id=?", (bot_id,))
        return

    await db_execute("UPDATE bots SET status='crashed' WHERE id=?", (bot_id,))

    if auto_restart:
        await asyncio.sleep(3)
        try:
            row = await get_bot(bot_id)
            if row:
                await start_hosted_bot(bot_id, auto_restart=True)
        except Exception as e:
            logger.exception("Auto restart failed for %s: %s", bot_id, e)


async def stop_hosted_bot(bot_id: int):
    async with PROCESS_LOCK:
        proc = processes.get(bot_id)
        if not proc or proc.returncode is not None:
            processes.pop(bot_id, None)
            await db_execute("UPDATE bots SET status='stopped' WHERE id=?", (bot_id,))
            return True, "Already stopped."

        stop_requested.add(bot_id)
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass

    try:
        await asyncio.wait_for(proc.wait(), timeout=8)
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    await db_execute("UPDATE bots SET status='stopped' WHERE id=?", (bot_id,))
    return True, "Stopped."


async def delete_hosted_bot(bot_id: int):
    row = await get_bot(bot_id)
    if not row:
        return False, "Bot not found."

    await stop_hosted_bot(bot_id)
    folder = HOST_ROOT / str(bot_id)
    shutil.rmtree(folder, ignore_errors=True)
    await db_execute("DELETE FROM bots WHERE id=?", (bot_id,))
    return True, "Deleted."


@dp.message(CommandStart())
async def start_cmd(message: Message):
    if not await guard(message):
        return
    await message.answer(
        "🚀 <b>MUSKAN HOSTING</b>\n\n"
        "Free Telegram Bot Hosting Panel\n\n"
        "➕ Add your Python bot\n"
        "▶️ Start • Stop • Restart\n"
        "📜 View logs\n"
        "🔄 Automatic crash restart\n\n"
        "Choose an option below:",
        reply_markup=main_keyboard(message.from_user.id)
    )


@dp.message(Command("admin"))
async def admin_cmd(message: Message):
    if not await guard(message):
        return
    if not is_admin(message.from_user.id):
        await message.answer("🚫 Admin only.")
        return
    await message.answer("👑 <b>Admin Panel</b>", reply_markup=admin_keyboard())


@dp.callback_query(F.data == "home")
async def home_cb(call: CallbackQuery):
    if await is_blocked(call.from_user.id):
        await call.answer("Blocked.", show_alert=True)
        return
    await call.message.edit_text(
        "🚀 <b>MUSKAN HOSTING</b>\n\nSelect an option:",
        reply_markup=main_keyboard(call.from_user.id)
    )
    await call.answer()


@dp.callback_query(F.data == "add_bot")
async def add_bot_cb(call: CallbackQuery):
    if await is_blocked(call.from_user.id):
        await call.answer("Blocked.", show_alert=True)
        return
    count = await bot_count(call.from_user.id)
    if count >= MAX_BOTS_PER_USER:
        await call.answer(f"Bot limit reached ({MAX_BOTS_PER_USER}).", show_alert=True)
        return
    await call.message.edit_text(
        "📤 <b>Upload your bot project as a ZIP file.</b>\n\n"
        "Required:\n"
        "• At least one .py file (main.py recommended)\n"
        "• requirements.txt is optional\n\n"
        f"Maximum ZIP size: {MAX_ZIP_MB} MB\n"
        "After upload, the bot will be installed and you can start it.",
        reply_markup=back_keyboard()
    )
    await call.answer()


@dp.message(F.document)
async def document_upload(message: Message):
    if not await guard(message):
        return

    doc = message.document
    if not doc.file_name.lower().endswith(".zip"):
        await message.answer("❌ Please upload a <b>.zip</b> project file.")
        return

    if doc.file_size and doc.file_size > MAX_ZIP_MB * 1024 * 1024:
        await message.answer(f"❌ ZIP is too large. Maximum is {MAX_ZIP_MB} MB.")
        return

    count = await bot_count(message.from_user.id)
    if count >= MAX_BOTS_PER_USER:
        await message.answer(f"❌ You reached your limit of {MAX_BOTS_PER_USER} hosted bots.")
        return

    temp = BASE_DIR / f"_upload_{message.from_user.id}_{int(time.time())}.zip"
    try:
        await bot.download(doc, destination=temp)

        # Validate archive before extraction.
        with zipfile.ZipFile(temp, "r") as zf:
            if not valid_zip_members(zf):
                await message.answer("❌ Unsafe ZIP structure detected.")
                return

            new_id = None
            async with DB_LOCK:
                conn = db_connect()
                cur = conn.execute(
                    "INSERT INTO bots(user_id,name,folder,entry_file,status,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (message.from_user.id, safe_name(Path(doc.file_name).stem), "", "",
                     "uploading", int(time.time()))
                )
                new_id = cur.lastrowid
                folder = HOST_ROOT / str(new_id)
                conn.execute(
                    "UPDATE bots SET folder=? WHERE id=?",
                    (str(folder), new_id)
                )
                conn.commit()
                conn.close()

            folder.mkdir(parents=True, exist_ok=True)
            zf.extractall(folder)

        # If ZIP has a single top-level directory, flatten it.
        entries = list(folder.iterdir())
        if len(entries) == 1 and entries[0].is_dir():
            inner = entries[0]
            for item in inner.iterdir():
                target = folder / item.name
                if target.exists():
                    if target.is_dir():
                        shutil.copytree(item, target, dirs_exist_ok=True)
                    else:
                        target.unlink()
                        shutil.copy2(item, target)
                else:
                    shutil.move(str(item), str(target))
            inner.rmdir()

        entry = await detect_entry(folder)
        if not entry:
            await delete_hosted_bot(new_id)
            await message.answer("❌ No Python entry file found in the ZIP.")
            return

        await db_execute(
            "UPDATE bots SET name=?, entry_file=?, status='stopped' WHERE id=?",
            (safe_name(Path(doc.file_name).stem), entry, new_id)
        )

        await message.answer(
            f"✅ <b>Bot uploaded successfully!</b>\n\n"
            f"🆔 Bot ID: <code>{new_id}</code>\n"
            f"📄 Entry: <code>{entry}</code>\n"
            f"📦 Name: <code>{safe_name(Path(doc.file_name).stem)}</code>",
            reply_markup=bot_action_keyboard(new_id, "stopped")
        )

    except zipfile.BadZipFile:
        await message.answer("❌ Invalid ZIP file.")
    except Exception as e:
        logger.exception("Upload error")
        await message.answer(f"❌ Upload failed:\n<code>{str(e)[:1200]}</code>")
        # Clean up orphan DB record/folder when possible.
    finally:
        temp.unlink(missing_ok=True)


@dp.callback_query(F.data == "my_bots")
async def my_bots_cb(call: CallbackQuery):
    if await is_blocked(call.from_user.id):
        await call.answer("Blocked.", show_alert=True)
        return

    rows = await get_user_bots(call.from_user.id)
    if not rows:
        await call.message.edit_text(
            "🤖 <b>My Bots</b>\n\nYou have no hosted bots yet.",
            reply_markup=main_keyboard(call.from_user.id)
        )
        await call.answer()
        return

    kb = InlineKeyboardBuilder()
    text = "🤖 <b>Your Hosted Bots</b>\n\n"
    for row in rows:
        text += f"🆔 <code>{row['id']}</code> • <b>{row['name']}</b> • {row['status']}\n"
        kb.button(text=f"{'🟢' if row['status']=='running' else '🔴'} {row['name']}", callback_data=f"view:{row['id']}")
    kb.button(text="🔙 Main Menu", callback_data="home")
    kb.adjust(1)
    await call.message.edit_text(text, reply_markup=kb.as_markup())
    await call.answer()


@dp.callback_query(F.data.startswith("view:"))
async def view_bot_cb(call: CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    row = await get_bot(bot_id)
    if not row or row["user_id"] != call.from_user.id:
        await call.answer("Not your bot.", show_alert=True)
        return
    await call.message.edit_text(
        f"🤖 <b>{row['name']}</b>\n\n"
        f"🆔 ID: <code>{row['id']}</code>\n"
        f"📄 Entry: <code>{row['entry_file']}</code>\n"
        f"📌 Status: <b>{row['status']}</b>",
        reply_markup=bot_action_keyboard(bot_id, row["status"])
    )
    await call.answer()


async def ownership(call: CallbackQuery, bot_id: int):
    row = await get_bot(bot_id)
    if not row:
        await call.answer("Bot not found.", show_alert=True)
        return None
    if row["user_id"] != call.from_user.id and not is_admin(call.from_user.id):
        await call.answer("Not your bot.", show_alert=True)
        return None
    return row


@dp.callback_query(F.data.startswith("start:"))
async def start_cb(call: CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    row = await ownership(call, bot_id)
    if not row:
        return
    ok, msg = await start_hosted_bot(bot_id)
    await call.answer(msg, show_alert=not ok)
    row = await get_bot(bot_id)
    await call.message.edit_reply_markup(reply_markup=bot_action_keyboard(bot_id, row["status"]))


@dp.callback_query(F.data.startswith("stop:"))
async def stop_cb(call: CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    row = await ownership(call, bot_id)
    if not row:
        return
    ok, msg = await stop_hosted_bot(bot_id)
    await call.answer(msg, show_alert=not ok)
    row = await get_bot(bot_id)
    await call.message.edit_reply_markup(reply_markup=bot_action_keyboard(bot_id, row["status"]))


@dp.callback_query(F.data.startswith("restart:"))
async def restart_cb(call: CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    row = await ownership(call, bot_id)
    if not row:
        return
    await stop_hosted_bot(bot_id)
    await asyncio.sleep(1)
    ok, msg = await start_hosted_bot(bot_id)
    await call.answer("🔄 " + msg, show_alert=not ok)
    row = await get_bot(bot_id)
    await call.message.edit_reply_markup(reply_markup=bot_action_keyboard(bot_id, row["status"]))


@dp.callback_query(F.data.startswith("logs:"))
async def logs_cb(call: CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    row = await ownership(call, bot_id)
    if not row:
        return
    logs = await read_logs(bot_id)
    if len(logs) > 3500:
        logs = logs[-3500:]
    await call.message.edit_text(
        f"📜 <b>Logs — {row['name']}</b>\n\n<pre>{logs}</pre>",
        reply_markup=bot_action_keyboard(bot_id, row["status"])
    )
    await call.answer()


@dp.callback_query(F.data.startswith("delete:"))
async def delete_cb(call: CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    row = await ownership(call, bot_id)
    if not row:
        return
    await delete_hosted_bot(bot_id)
    await call.message.edit_text("🗑️ <b>Bot deleted successfully.</b>", reply_markup=main_keyboard(call.from_user.id))
    await call.answer()


@dp.callback_query(F.data == "howto")
async def howto_cb(call: CallbackQuery):
    await call.message.edit_text(
        "📖 <b>How To Use</b>\n\n"
        "1. Tap ➕ Add Bot.\n"
        "2. Upload your Python project as ZIP.\n"
        "3. Include main.py (recommended).\n"
        "4. Put dependencies in requirements.txt if needed.\n"
        "5. Open My Bots and press ▶️ Start.\n"
        "6. Use Logs if the bot has an error.\n\n"
        "⚠️ Never upload secrets you do not own.",
        reply_markup=back_keyboard()
    )
    await call.answer()


@dp.callback_query(F.data == "support")
async def support_cb(call: CallbackQuery):
    await call.message.edit_text(
        "💬 <b>Support Center</b>\n\n"
        "Contact your hosting administrator for support.",
        reply_markup=back_keyboard()
    )
    await call.answer()


@dp.callback_query(F.data == "admin")
async def admin_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only.", show_alert=True)
        return
    await call.message.edit_text("👑 <b>Admin Panel</b>", reply_markup=admin_keyboard())
    await call.answer()


@dp.callback_query(F.data == "admin_dashboard")
async def admin_dashboard_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only.", show_alert=True)
        return
    users = await db_execute("SELECT COUNT(*) c FROM users", one=True)
    bots_count = await db_execute("SELECT COUNT(*) c FROM bots", one=True)
    running = await db_execute("SELECT COUNT(*) c FROM bots WHERE status='running'", one=True)
    await call.message.edit_text(
        "📊 <b>Dashboard</b>\n\n"
        f"👥 Users: <b>{users['c']}</b>\n"
        f"🤖 Hosted Bots: <b>{bots_count['c']}</b>\n"
        f"🟢 Running: <b>{running['c']}</b>\n"
        f"🔴 Not Running: <b>{bots_count['c'] - running['c']}</b>",
        reply_markup=admin_keyboard()
    )
    await call.answer()


async def admin_bot_list(call: CallbackQuery, status_filter=None):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only.", show_alert=True)
        return
    if status_filter:
        rows = await db_execute(
            "SELECT * FROM bots WHERE status=? ORDER BY id DESC",
            (status_filter,), fetch=True
        )
    else:
        rows = await db_execute("SELECT * FROM bots ORDER BY id DESC", fetch=True)

    if not rows:
        await call.message.edit_text("🤖 No bots found.", reply_markup=admin_keyboard())
        await call.answer()
        return

    text = "🤖 <b>All Hosted Bots</b>\n\n"
    for row in rows[:50]:
        text += (
            f"🆔 <code>{row['id']}</code> | "
            f"User <code>{row['user_id']}</code> | "
            f"{row['name']} | <b>{row['status']}</b>\n"
        )
    if len(rows) > 50:
        text += "\nShowing latest 50."
    await call.message.edit_text(text, reply_markup=admin_keyboard())
    await call.answer()


@dp.callback_query(F.data == "admin_bots")
async def admin_bots_cb(call: CallbackQuery):
    await admin_bot_list(call)


@dp.callback_query(F.data == "admin_running")
async def admin_running_cb(call: CallbackQuery):
    await admin_bot_list(call, "running")


@dp.callback_query(F.data == "admin_stopped")
async def admin_stopped_cb(call: CallbackQuery):
    await admin_bot_list(call, "stopped")


@dp.callback_query(F.data == "admin_users")
async def admin_users_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only.", show_alert=True)
        return
    rows = await db_execute(
        "SELECT user_id,username,first_name,blocked FROM users ORDER BY created_at DESC LIMIT 50",
        fetch=True
    )
    text = "👥 <b>Users</b>\n\n"
    if not rows:
        text += "No users."
    else:
        for r in rows:
            state = "🚫" if r["blocked"] else "✅"
            text += f"{state} <code>{r['user_id']}</code> @{r['username'] or '-'}\n"
    await call.message.edit_text(text, reply_markup=admin_keyboard())
    await call.answer()


async def restore_running_bots():
    rows = await db_execute(
        "SELECT id FROM bots WHERE status='running'",
        fetch=True
    )
    for row in rows:
        # Do not block startup on one broken bot.
        try:
            await start_hosted_bot(row["id"], auto_restart=True)
        except Exception:
            logger.exception("Could not restore bot %s", row["id"])
            await db_execute("UPDATE bots SET status='crashed' WHERE id=?", (row["id"],))


async def main():
    init_db()
    logger.info("MUSKAN HOSTING starting...")
    await restore_running_bots()
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("MUSKAN HOSTING stopped.")
