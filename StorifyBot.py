#!/usr/bin/env python3
import nest_asyncio
nest_asyncio.apply()

import os
import logging
from pathlib import Path
from typing import Optional, List, Tuple
from collections import defaultdict

import aiosqlite
from passlib.hash import sha256_crypt

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

# ============== CONFIG ==============
TOKEN = "7368089132:AAF0XfAWzquY5Wg7EClz3UgsjZiYbGjG7C8"  # Замени на свой токен!
DB_FILE = "storify.db"
MEDIA_DIR = Path("media")
MEDIA_DIR.mkdir(exist_ok=True)
BATCH_SIZE = 8
MAX_SEARCH_RESULTS = 30

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ============== EMOJIS ==============
E = {
    "success": "✔️", "error": "❌", "warning": "⚠️", "search": "🔎", "add": "➕",
    "save": "💾", "send": "📤", "menu": "🏠", "back": "◀️", "login": "🔐",
    "logout": "🚪", "delete": "🗑️", "clear": "🧹", "note": "📝", "wave": "👋",
    "settings": "⚙️", "new": "🆕", "key": "🔑", "list": "📋",
}

# ============== STATES ==============
(
    CREATE_USER, CREATE_PASS,
    LOGIN_USER, LOGIN_PASS,
    ADD_NOTE,
    ADD_NOTE_AWAIT_GROUP,
    ADD_NOTE_AWAIT_GROUP_TEXT,
    AWAIT_KEYWORD,
    CHOOSE_GROUP_SEARCH, SEARCH,
    CONFIRM_CLEAR, CONFIRM_DELETE,
    DELETE_NOTE_CONFIRM,
    DELETE_GROUP_SELECT, DELETE_GROUP_CONFIRM
) = range(15)

# ============== DATABASE ==============
async def init_db() -> None:
    async with aiosqlite.connect(DB_FILE) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users(
                username TEXT PRIMARY KEY COLLATE NOCASE,
                pass_hash TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions(
                tg_id INTEGER PRIMARY KEY,
                username TEXT,
                FOREIGN KEY(username) REFERENCES users(username)
            );
            CREATE TABLE IF NOT EXISTS notes(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                keyword TEXT,
                type TEXT NOT NULL,
                text TEXT,
                file_path TEXT,
                caption TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                group_name TEXT,
                FOREIGN KEY(username) REFERENCES users(username)
            );
        """)
        await db.commit()

        cur = await db.execute("PRAGMA table_info(notes)")
        columns = [row[1] for row in await cur.fetchall()]

        if "keyword" not in columns:
            await db.execute("ALTER TABLE notes ADD COLUMN keyword TEXT")
            await db.commit()

        await db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_keyword_per_user
            ON notes(username, lower(keyword))
        """)
        await db.commit()

async def create_user(username: str, password: str) -> bool:
    if not username or len(password) < 4:
        return False
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "INSERT INTO users(username, pass_hash) VALUES (?, ?)",
                (username, sha256_crypt.hash(password))
            )
            await db.commit()
        return True
    except aiosqlite.IntegrityError:
        return False

async def verify_user(username: str, password: str) -> Optional[str]:
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT username, pass_hash FROM users WHERE lower(username) = lower(?)", (username,))
        row = await cur.fetchone()
    if row and sha256_crypt.verify(password, row[1]):
        return row[0]
    return None

async def set_session(tg_id: int, username: Optional[str]) -> None:
    async with aiosqlite.connect(DB_FILE) as db:
        if username is None:
            await db.execute("DELETE FROM sessions WHERE tg_id = ?", (tg_id,))
        else:
            await db.execute("REPLACE INTO sessions(tg_id, username) VALUES (?, ?)", (tg_id, username))
        await db.commit()

async def get_session_username(tg_id: int) -> Optional[str]:
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT username FROM sessions WHERE tg_id = ?", (tg_id,))
        row = await cur.fetchone()
    return row[0] if row else None

async def add_note(
    username: str, keyword: str, ntype: str,
    text: Optional[str] = None, file_path: Optional[str] = None,
    caption: Optional[str] = None, group_name: Optional[str] = None
) -> bool:
    keyword = keyword.strip().lower()
    if not keyword:
        return False
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "INSERT INTO notes(username, keyword, type, text, file_path, caption, group_name) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (username, keyword, ntype, text, file_path, caption, group_name)
            )
            await db.commit()
        return True
    except aiosqlite.IntegrityError:
        return False

async def get_note_by_keyword(username: str, keyword: str) -> Optional[Tuple]:
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            "SELECT * FROM notes WHERE username = ? AND lower(keyword) = ?",
            (username, keyword.lower())
        )
        return await cur.fetchone()

async def delete_note_by_keyword(username: str, keyword: str) -> bool:
    keyword = keyword.strip().lower()
    note = await get_note_by_keyword(username, keyword)
    if not note:
        return False
    fpath = note[5]
    if fpath and Path(fpath).exists():
        try:
            Path(fpath).unlink()
        except:
            pass
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM notes WHERE username = ? AND lower(keyword) = ?", (username, keyword))
        await db.commit()
    return True

async def delete_group(username: str, group_name: str) -> int:
    notes = await get_notes(username, group_name=group_name)
    for note in notes:
        fpath = note[5]
        if fpath and Path(fpath).exists():
            try:
                Path(fpath).unlink()
            except:
                pass
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM notes WHERE username = ? AND group_name = ?", (username, group_name))
        await db.commit()
    return len(notes)

async def get_notes(username: str, group_name: Optional[str] = None) -> List[Tuple]:
    async with aiosqlite.connect(DB_FILE) as db:
        if group_name and group_name != "__all__":
            cur = await db.execute(
                "SELECT * FROM notes WHERE username = ? AND group_name = ? ORDER BY id DESC",
                (username, group_name)
            )
        else:
            cur = await db.execute(
                "SELECT * FROM notes WHERE username = ? ORDER BY id DESC",
                (username,)
            )
        return await cur.fetchall()

async def search_notes(username: str, query: str, group_name: Optional[str] = None) -> List[Tuple]:
    like = f"%{query.lower()}%"
    async with aiosqlite.connect(DB_FILE) as db:
        if group_name and group_name != "__all__":
            cur = await db.execute(
                "SELECT * FROM notes WHERE username = ? AND group_name = ? AND "
                "(lower(text) LIKE ? OR lower(caption) LIKE ? OR lower(keyword) LIKE ?) "
                "ORDER BY id DESC",
                (username, group_name, like, like, like)
            )
        else:
            cur = await db.execute(
                "SELECT * FROM notes WHERE username = ? AND "
                "(lower(text) LIKE ? OR lower(caption) LIKE ? OR lower(keyword) LIKE ?) "
                "ORDER BY id DESC",
                (username, like, like, like)
            )
        return await cur.fetchall()

async def get_groups(username: str) -> List[str]:
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            "SELECT DISTINCT group_name FROM notes WHERE username = ? AND group_name IS NOT NULL",
            (username,)
        )
        rows = await cur.fetchall()
    return sorted([row[0] for row in rows])

async def get_keywords_list(username: str) -> str:
    notes = await get_notes(username)
    if not notes:
        return "You have no notes yet."

    grouped = defaultdict(list)
    for note in notes:
        keyword = note[2]
        group = note[8] if len(note) > 8 else None
        group_name = group or "Without group"
        grouped[group_name].append(keyword)

    lines = [f"<b>Your keywords ({len(notes)} total):</b>\n"]
    for group_name in sorted(grouped.keys()):
        keywords = sorted(grouped[group_name])
        prefix = "📌 " if group_name == "Without group" else "📁 "
        lines.append(f"{prefix}<b>{group_name}</b>:")
        for kw in keywords:
            lines.append(f"  • <code>{kw}</code>")
        lines.append("")
    return "\n".join(lines)

async def clear_user_notes(username: str) -> None:
    notes = await get_notes(username)
    for note in notes:
        fpath = note[5]
        if fpath and Path(fpath).exists():
            try:
                Path(fpath).unlink()
            except Exception:
                pass
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM notes WHERE username = ?", (username,))
        await db.commit()

async def delete_user_data(username: str) -> None:
    await clear_user_notes(username)
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM users WHERE username = ?", (username,))
        await db.execute("DELETE FROM sessions WHERE username = ?", (username,))
        await db.commit()

# ============== MEDIA ==============
def safe_filename(file_unique_id: str) -> str:
    return file_unique_id

async def download_file(file_obj, file_unique_id: str) -> str:
    path = MEDIA_DIR / safe_filename(file_unique_id)
    await file_obj.download_to_drive(path)
    return str(path)

async def send_note(bot, chat_id: int, note: Tuple) -> None:
    keyword = note[2]
    ntype = note[3]
    text = note[4]
    file_path = note[5]
    caption = note[6] or ""
    group_name = note[8] if len(note) > 8 else None

    prefix = f"[{group_name}] " if group_name else ""
    keyword_line = f"<b>Keyword:</b> <code>{keyword}</code>\n"
    full_caption = f"{prefix}{keyword_line}{caption}".strip()

    try:
        if ntype == "text":
            message = keyword_line + (text or "[empty text note]")
            await bot.send_message(chat_id, message, parse_mode="HTML")
        elif ntype == "photo" and file_path and Path(file_path).exists():
            with open(file_path, "rb") as f:
                await bot.send_photo(chat_id, InputFile(f), caption=full_caption, parse_mode="HTML")
        elif ntype in ("video", "video_note") and file_path and Path(file_path).exists():
            with open(file_path, "rb") as f:
                await bot.send_video(chat_id, InputFile(f), caption=full_caption, parse_mode="HTML")
        elif ntype == "document" and file_path and Path(file_path).exists():
            with open(file_path, "rb") as f:
                await bot.send_document(chat_id, InputFile(f), caption=full_caption, parse_mode="HTML")
        elif ntype == "voice" and file_path and Path(file_path).exists():
            with open(file_path, "rb") as f:
                await bot.send_voice(chat_id, InputFile(f), caption=full_caption, parse_mode="HTML")
        else:
            await bot.send_message(chat_id, f"<code>{keyword}</code> [media missing]", parse_mode="HTML")
    except Exception as e:
        logger.error(f"Send note failed: {e}")

# ============== KEYBOARDS ==============
def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{E['add']} Add Note", callback_data="add")],
        [InlineKeyboardButton(f"{E['list']} List Keywords", callback_data="list_keywords")],
        [InlineKeyboardButton(f"{E['delete']} Delete Note", callback_data="delete_note")],
        [InlineKeyboardButton(f"{E['delete']} Delete Group", callback_data="delete_group")],
        [InlineKeyboardButton(f"{E['send']} Get All", callback_data="get_all")],
        [InlineKeyboardButton(f"{E['search']} Search", callback_data="search")],
        [InlineKeyboardButton(f"{E['clear']} Clear All", callback_data="clear")],
        [InlineKeyboardButton(f"{E['settings']} Account", callback_data="account")],
    ])

def auth_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{E['new']} Create Account", callback_data="create")],
        [InlineKeyboardButton(f"{E['login']} Log In", callback_data="login")],
    ])

def back_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"{E['back']} Back", callback_data="main")]])

def confirm_buttons(yes_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Yes, confirm", callback_data=yes_data),
         InlineKeyboardButton("No", callback_data="main")]
    ])

def groups_keyboard(groups: List[str], prefix: str, include_all: bool = True, include_none: bool = False) -> InlineKeyboardMarkup:
    kb = []
    if include_all:
        kb.append([InlineKeyboardButton("📂 All groups", callback_data=f"{prefix}|__all__")])
    if include_none:
        kb.append([InlineKeyboardButton("📌 Without group", callback_data=f"{prefix}|__none__")])
    for g in groups:
        kb.append([InlineKeyboardButton(g, callback_data=f"{prefix}|{g}")])
    kb.append([InlineKeyboardButton("🆕 Type new group", callback_data=f"{prefix}|__type__")])
    kb.append([InlineKeyboardButton(f"{E['back']} Back", callback_data="main")])
    return InlineKeyboardMarkup(kb)

# ============== HANDLERS ==============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    username = await get_session_username(tg_id)
    if update.callback_query:
        await update.callback_query.answer()

    if username:
        text = f"{E['success']} Logged in as <b>{username}</b>"
        kb = main_menu()
    else:
        text = f"{E['wave']} Welcome to <b>Storify</b>\nYour private vault with custom keywords!"
        kb = auth_menu()

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await update.message.reply_html(text, reply_markup=kb)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("Cancelled.", reply_markup=main_menu())
    else:
        await update.message.reply_text("Cancelled.", reply_markup=main_menu())
    return ConversationHandler.END

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    tg_id = query.from_user.id
    username = await get_session_username(tg_id)

    if data == "main":
        await start(update, context)
        return ConversationHandler.END

    if data in ("create", "login"):
        await query.edit_message_text(f"{E['new'] if data == 'create' else E['login']} Send username:")
        return CREATE_USER if data == "create" else LOGIN_USER

    if not username:
        await query.edit_message_text(f"{E['error']} Please log in first!", reply_markup=auth_menu())
        return ConversationHandler.END

    if data == "list_keywords":
        keywords_text = await get_keywords_list(username)
        await query.edit_message_text(keywords_text, parse_mode="HTML", reply_markup=main_menu())
        return ConversationHandler.END

    if data == "add":
        await query.edit_message_text(f"{E['add']} Send your note (text, photo, video, voice, file…)", reply_markup=back_button())
        return ADD_NOTE

    if data == "delete_note":
        await query.edit_message_text(f"{E['delete']} Send the keyword of the note you want to delete:", reply_markup=back_button())
        return DELETE_NOTE_CONFIRM

    if data == "delete_group":
        groups = await get_groups(username)
        if not groups:
            await query.edit_message_text("You have no groups to delete.", reply_markup=main_menu())
            return ConversationHandler.END
        await query.edit_message_text("Choose group to delete (all notes in it will be deleted):", reply_markup=groups_keyboard(groups, "delgroup", include_all=False, include_none=False))
        return DELETE_GROUP_SELECT

    if data.startswith("delgroup|"):
        group = data.split("|", 1)[1]
        context.user_data["group_to_delete"] = group
        await query.edit_message_text(
            f"{E['warning']} Delete group <b>{group}</b> and ALL notes in it?\nThis cannot be undone!",
            reply_markup=confirm_buttons("confirm_delete_group"),
            parse_mode="HTML"
        )
        return DELETE_GROUP_CONFIRM

    if data == "confirm_delete_group":
        group = context.user_data.pop("group_to_delete", None)
        if not group:
            await query.edit_message_text("Error. Try again.", reply_markup=main_menu())
            return ConversationHandler.END
        deleted_count = await delete_group(username, group)
        await query.edit_message_text(
            f"{E['delete']} Group <b>{group}</b> deleted!\nRemoved {deleted_count} note(s).",
            reply_markup=main_menu(),
            parse_mode="HTML"
        )
        return ConversationHandler.END

    if data == "get_all":
        groups = await get_groups(username)
        await query.edit_message_text("Choose group to view:", reply_markup=groups_keyboard(groups, "get_all", include_all=True, include_none=True))
        return ConversationHandler.END

    if data.startswith("get_all|"):
        sel = data.split("|", 1)[1]
        group = None if sel in ("__all__", "__none__") else sel
        await send_all_notes(query, context, username, group)
        return ConversationHandler.END

    if data == "search":
        groups = await get_groups(username)
        await query.edit_message_text("Search in which group?", reply_markup=groups_keyboard(groups, "search_group", include_all=True, include_none=True))
        return ConversationHandler.END

    if data.startswith("search_group|"):
        sel = data.split("|", 1)[1]
        if sel == "__type__":
            context.user_data["pending_group_flow"] = "search"
            await query.edit_message_text("Type group name to search in:")
            return CHOOSE_GROUP_SEARCH
        else:
            group = None if sel in ("__all__", "__none__") else sel
            context.user_data["search_group"] = group
            group_display = "All groups" if sel == "__all__" else ("Without group" if sel == "__none__" else sel)
            await query.edit_message_text(
                f"{E['search']} Send a keyword to find your note in <b>{group_display}</b>:",
                parse_mode="HTML", reply_markup=back_button()
            )
            return SEARCH

    if data.startswith("add_group|"):
        sel = data.split("|", 1)[1]
        pending = context.user_data.get("pending_note")
        if not pending:
            await query.edit_message_text(f"{E['error']} No pending note.", reply_markup=main_menu())
            return ConversationHandler.END

        if sel == "__type__":
            context.user_data["pending_group_flow"] = "add"
            await query.edit_message_text("Type the new group name:")
            return ADD_NOTE_AWAIT_GROUP_TEXT
        elif sel == "__none__":
            group = None
        else:
            group = sel
        context.user_data["selected_group"] = group
        await query.edit_message_text(f"{E['key']} Now send a keyword for this note\n(one word recommended):")
        return AWAIT_KEYWORD

    if data == "clear":
        await query.edit_message_text(f"{E['warning']} Delete ALL your notes?", reply_markup=confirm_buttons("do_clear"))
        return CONFIRM_CLEAR

    if data == "do_clear":
        await clear_user_notes(username)
        await query.edit_message_text(f"{E['clear']} All notes deleted!", reply_markup=main_menu())
        return ConversationHandler.END

    if data == "account":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{E['logout']} Log Out", callback_data="logout")],
            [InlineKeyboardButton(f"{E['delete']} Delete Account", callback_data="delete_acc")],
            [InlineKeyboardButton(f"{E['back']} Back", callback_data="main")],
        ])
        await query.edit_message_text(f"<b>{username}</b> Account Settings", reply_markup=kb, parse_mode="HTML")
        return ConversationHandler.END

    if data == "logout":
        await set_session(tg_id, None)
        await query.edit_message_text(f"{E['logout']} Logged out successfully.", reply_markup=auth_menu())
        return ConversationHandler.END

    if data == "delete_acc":
        await query.edit_message_text(f"{E['delete']} Type your username <code>{username}</code> to confirm deletion:", parse_mode="HTML")
        return CONFIRM_DELETE

    return ConversationHandler.END

# ============== STATE HANDLERS ==============
async def create_user_state(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    username = update.message.text.strip()
    if not username or len(username) > 50:
        await update.message.reply_text(f"{E['warning']} Invalid username.")
        return CREATE_USER
    context.user_data["temp_user"] = username
    await update.message.reply_text(f"{E['login']} Send password:")
    return CREATE_PASS

async def create_pass_state(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    password = update.message.text
    if len(password) < 4:
        await update.message.reply_text(f"{E['warning']} Password too short.")
        return CREATE_PASS
    username = context.user_data.pop("temp_user", None)
    if await create_user(username, password):
        await set_session(update.effective_user.id, username)
        await update.message.reply_html(f"{E['success']} Account <b>{username}</b> created!", reply_markup=main_menu())
    else:
        await update.message.reply_text(f"{E['error']} Username already taken.")
    return ConversationHandler.END

async def login_user_state(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["temp_user"] = update.message.text.strip()
    await update.message.reply_text(f"{E['login']} Send password:")
    return LOGIN_PASS

async def login_pass_state(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    username = context.user_data.pop("temp_user", None)
    password = update.message.text
    verified = await verify_user(username, password)
    if verified:
        await set_session(update.effective_user.id, verified)
        await update.message.reply_html(f"{E['success']} Logged in as <b>{verified}</b>", reply_markup=main_menu())
    else:
        await update.message.reply_text(f"{E['error']} Wrong credentials.")
    return ConversationHandler.END

async def add_note_state(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    username = await get_session_username(update.effective_user.id)
    if not username:
        await update.message.reply_text(f"{E['error']} Login required.")
        return ConversationHandler.END

    msg = update.message
    ntype = "text"
    text = msg.text
    file_path = None
    caption = msg.caption

    try:
        if msg.photo:
            file = await msg.photo[-1].get_file()
            file_path = await download_file(file, msg.photo[-1].file_unique_id)
            ntype = "photo"
        elif msg.video or msg.video_note:
            file_obj = msg.video or msg.video_note
            file = await file_obj.get_file()
            file_path = await download_file(file, file_obj.file_unique_id)
            ntype = "video" if msg.video else "video_note"
        elif msg.document:
            file = await msg.document.get_file()
            file_path = await download_file(file, msg.document.file_unique_id)
            ntype = "document"
        elif msg.voice:
            file = await msg.voice.get_file()
            file_path = await download_file(file, msg.voice.file_unique_id)
            ntype = "voice"

        context.user_data["pending_note"] = {
            "ntype": ntype,
            "text": text,
            "file_path": file_path,
            "caption": caption,
        }

        groups = await get_groups(username)
        await update.message.reply_text(
            "Choose group to save this note:",
            reply_markup=groups_keyboard(groups, "add_group", include_all=False, include_none=True)
        )
        return ADD_NOTE_AWAIT_GROUP
    except Exception as e:
        logger.exception(e)
        await update.message.reply_text(f"{E['error']} Failed to process content.")
        return ConversationHandler.END

async def add_note_await_group_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    group_name = update.message.text.strip()
    if not group_name:
        await update.message.reply_text(f"{E['warning']} Group name cannot be empty.")
        return ADD_NOTE_AWAIT_GROUP_TEXT

    context.user_data["selected_group"] = group_name
    await update.message.reply_text(f"{E['key']} Now send a keyword for this note:")
    return AWAIT_KEYWORD

async def await_keyword(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyword = update.message.text.strip().lower()
    if not keyword or len(keyword) > 50 or " " in keyword:
        await update.message.reply_text(f"{E['warning']} Keyword must be one word, no spaces.")
        return AWAIT_KEYWORD

    pending = context.user_data.get("pending_note")
    if not pending:
        await update.message.reply_text(f"{E['error']} No pending note.")
        return ConversationHandler.END

    username = await get_session_username(update.effective_user.id)
    group = context.user_data.get("selected_group")

    success = await add_note(
        username=username,
        keyword=keyword,
        ntype=pending["ntype"],
        text=pending.get("text"),
        file_path=pending.get("file_path"),
        caption=pending.get("caption"),
        group_name=group
    )

    context.user_data.clear()

    if success:
        group_display = "Without group" if group is None else group
        group_text = f" in group <b>{group_display}</b>" if group is not None else f" (<i>{group_display.lower()}</i>)"
        await update.message.reply_html(
            f"{E['save']} Note saved with keyword <code>{keyword}</code>{group_text}!\n\n"
            f"Just send <code>{keyword}</code> anytime to view it.",
            reply_markup=main_menu()
        )
    else:
        await update.message.reply_text(
            f"{E['error']} This keyword is already used. Choose another.",
            reply_markup=main_menu()
        )
    return ConversationHandler.END

async def delete_note_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    username = await get_session_username(update.effective_user.id)
    keyword = update.message.text.strip().lower()

    success = await delete_note_by_keyword(username, keyword)
    if success:
        await update.message.reply_html(
            f"{E['delete']} Note with keyword <code>{keyword}</code> deleted permanently!",
            reply_markup=main_menu()
        )
    else:
        await update.message.reply_text(
            f"{E['error']} No note found with keyword <code>{keyword}</code>.",
            reply_markup=main_menu(),
            parse_mode="HTML"
        )
    return ConversationHandler.END

async def search_state(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    username = await get_session_username(update.effective_user.id)
    query = update.message.text.strip().lower()

    if context.user_data.get("pending_group_flow") == "search":
        context.user_data["search_group"] = query if query else None
        context.user_data.pop("pending_group_flow", None)
        group_display = query or "Without group"
        await update.message.reply_text(f"{E['search']} Now send keyword to search in <b>{group_display}</b>:", parse_mode="HTML")
        return SEARCH

    note = await get_note_by_keyword(username, query)
    group = context.user_data.get("search_group")

    if note:
        await send_note(context.bot, update.effective_chat.id, note)
        group_display = group or "Without group"
        await update.message.reply_html(
            f"{E['success']} Found with keyword <code>{query}</code> in <i>{group_display}</i>",
            reply_markup=main_menu()
        )
        context.user_data.pop("search_group", None)
        return ConversationHandler.END

    notes = await search_notes(username, query, group_name=group)

    if not notes:
        await update.message.reply_text(f"{E['search']} No results found.", reply_markup=main_menu())
        context.user_data.pop("search_group", None)
        return ConversationHandler.END

    for note in notes[:MAX_SEARCH_RESULTS]:
        await send_note(context.bot, update.effective_chat.id, note)

    await update.message.reply_text(f"{E['search']} Found {len(notes)} result(s).", reply_markup=main_menu())
    context.user_data.pop("search_group", None)
    return ConversationHandler.END

async def confirm_delete_state(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    username = await get_session_username(update.effective_user.id)
    if update.message.text.strip().lower() != username.lower():
        await update.message.reply_text(f"{E['error']} Incorrect username.")
        return CONFIRM_DELETE
    await delete_user_data(username)
    await update.message.reply_text(f"{E['delete']} Account and all data deleted permanently.")
    return ConversationHandler.END

async def send_all_notes(target, context: ContextTypes.DEFAULT_TYPE, username: str, group_name: Optional[str] = None):
    notes = await get_notes(username, group_name=group_name)
    if not notes:
        text = "You have no notes yet."
        if group_name is not None:
            text += f" in group <b>{group_name or 'Without group'}</b>."
        text += "."
        if hasattr(target, "edit_message_text"):
            await target.edit_message_text(text, reply_markup=main_menu(), parse_mode="HTML")
        else:
            await target.reply_html(text, reply_markup=main_menu())
        return

    chat_id = target.message.chat_id if hasattr(target, "message") else target.chat.id

    for i in range(0, len(notes), BATCH_SIZE):
        for note in notes[i:i + BATCH_SIZE]:
            await send_note(context.bot, chat_id, note)

    group_display = group_name or "Without group"
    success_text = f"{E['success']} Sent {len(notes)} note(s) from <b>{group_display}</b>."

    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(success_text, reply_markup=main_menu(), parse_mode="HTML")
    else:
        await target.reply_html(success_text, reply_markup=main_menu())

# ============== MAIN ==============
async def main():
    await init_db()
    app = Application.builder().token(TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start), CallbackQueryHandler(button)],
        states={
            CREATE_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_user_state)],
            CREATE_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_pass_state)],
            LOGIN_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_user_state)],
            LOGIN_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_pass_state)],
            ADD_NOTE: [MessageHandler(filters.ALL & ~filters.COMMAND, add_note_state)],
            ADD_NOTE_AWAIT_GROUP: [CallbackQueryHandler(button, pattern="^add_group\\|")],
            ADD_NOTE_AWAIT_GROUP_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_note_await_group_text)],
            AWAIT_KEYWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, await_keyword)],
            CHOOSE_GROUP_SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, search_state)],
            SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, search_state)],
            CONFIRM_CLEAR: [CallbackQueryHandler(button, pattern="^do_clear$")],
            CONFIRM_DELETE: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_delete_state)],
            DELETE_NOTE_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, delete_note_confirm)],
            DELETE_GROUP_SELECT: [CallbackQueryHandler(button, pattern="^delgroup\\|")],
            DELETE_GROUP_CONFIRM: [CallbackQueryHandler(button, pattern="^confirm_delete_group$")],
        },
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(button, pattern="^main$")],
        allow_reentry=True,
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("start", start))

    print("Storify Bot — Final Version with List Keywords & Always Show Keyword — Started!")
    await app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
