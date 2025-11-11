# main.py (All-in-One Bot: Quiz + Leaderboard + DB)

import telegram
from telegram import Update, constants, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, 
    CommandHandler, 
    ContextTypes, 
    MessageHandler, 
    filters, 
    CallbackQueryHandler
)
from telegram.helpers import escape_markdown
import requests
import random
import os
import asyncio
import html 
from datetime import datetime, timezone 
import logging 
import traceback
import json
import time 
import uuid 
import psycopg2 # 💡 DB ke liye
from urllib.parse import urlparse # 💡 DB URL ke liye

# --- ⚙️ Constants and Setup ---
GLOBAL_QUIZ_COOLDOWN = 600 # 10 minute global cooldown

# DB Keys
LOCK_KEY = 'global_quiz_lock' 
LAST_GLOBAL_QUIZ_KEY = 'last_global_quiz_time'
LAST_QUIZ_MESSAGE_KEY = 'last_quiz_message_ids'
OPEN_QUIZZES_KEY = 'open_quizzes_buttons' 
VIDEO_COUNTER_KEY = 'video_counter'

WELCOME_VIDEO_URLS = [
    "BAACAgUAAxkBAAIBxWkOFRIw0g1B_7xuSA4cyUE3ShSlAAJKGwAC_MFxVIx6wUDn1qKBNgQ",
    "BAACAgUAAxkBAAIByGkOFV746X7wbcPRCZTVy4iqtaC7AAKmIQACGV5QVJYHM5LZKdVANgQ",
]

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Environment Variables ---
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
WEBHOOK_URL = os.environ.get('RENDER_EXTERNAL_URL') 
OWNER_ID = os.environ.get('OWNER_ID')
PEXELS_API_KEY = os.environ.get('PEXELS_API_KEY')
STABLE_HORDE_API_KEY = os.environ.get('STABLE_HORDE_API_KEY', '0000000000')
START_PHOTO_ID = os.environ.get('START_PHOTO_ID') 
ABOUT_PHOTO_ID = os.environ.get('ABOUT_PHOTO_ID') 
DATABASE_URL = os.environ.get('DATABASE_URL') # Neon/PostgreSQL URL

# --- SPAM CONSTANTS ---
SPAM_MESSAGE_LIMIT = 5 # 5 messages
SPAM_TIME_WINDOW = 5 # in 5 seconds
SPAM_BLOCK_DURATION = 1200 # 20 min

# --- Error Handler ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)
    tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
    tb_string = "".join(tb_list)
    update_str = update.to_dict() if isinstance(update, Update) else str(update)
    message = (
        f"An exception was raised while handling an update\n"
        f"<pre>update = {html.escape(json.dumps(update_str, indent=2, ensure_ascii=False))}</pre>\n\n"
        f"<pre>context.chat_data = {html.escape(str(context.chat_data))}</pre>\n\n"
        f"<pre>context.user_data = {html.escape(str(context.user_data))}</pre>\n\n"
        f"<pre>{html.escape(tb_string)}</pre>"
    )
    logger.error(message) 
    if update and isinstance(update, Update) and update.effective_chat:
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="😕 Oops! Something went wrong. I've reported this to the developer.",
                parse_mode=constants.ParseMode.HTML
            )
        except Exception as e:
            logger.error(f"Failed to send error message to chat: {e}")

# ======================================================================
# --- 🗄️ DATABASE FUNCTIONS (Pehle leaderboard_manager mein thay) ---
# ======================================================================

def get_db_connection():
    """Establishes a connection to the PostgreSQL database."""
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL environment variable is not set.")
    try:
        result = urlparse(DATABASE_URL)
        username = result.username
        password = result.password
        database = result.path[1:]
        hostname = result.hostname
        port = result.port
        conn = psycopg2.connect(
            dbname=database,
            user=username,
            password=password,
            host=hostname,
            port=port
        )
        return conn
    except Exception as e:
        logger.error(f"Failed to connect to database: {e}")
        raise

def setup_database():
    """Initializes the database tables if they don't exist."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # 1. bot_data (Key-Value store)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_data (
                key TEXT PRIMARY KEY,
                value JSONB
            );
        """)
        
        # 2. user_data (User info, scores, spam)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_data (
                user_id TEXT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                message_count INTEGER DEFAULT 0,
                quiz_score INTEGER DEFAULT 0,
                spam_blocked_until FLOAT DEFAULT 0,
                spam_timestamps JSONB DEFAULT '[]'
            );
        """)
        
        # 3. chat_data (Active chats for broadcast)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS chat_data (
                chat_id TEXT PRIMARY KEY,
                title TEXT,
                is_active BOOLEAN DEFAULT TRUE
            );
        """)
        
        conn.commit()
        cur.close()
        logger.info("Database tables verified/created successfully.")
    except Exception as e:
        logger.error(f"Error during database setup: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()

# --- Key-Value Functions (bot_data) ---

def get_bot_value(key, default=None):
    """Gets a value from the bot_data table."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT value FROM bot_data WHERE key = %s", (key,))
        result = cur.fetchone()
        cur.close()
        if result:
            return result[0]
        return default
    except Exception as e:
        logger.error(f"Error getting bot value for key '{key}': {e}")
        return default
    finally:
        if conn:
            conn.close()

def set_bot_value(key, value):
    """Sets a value in the bot_data table."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # Use JSONB for efficient storage
        cur.execute("""
            INSERT INTO bot_data (key, value)
            VALUES (%s, %s)
            ON CONFLICT (key)
            DO UPDATE SET value = EXCLUDED.value;
        """, (key, json.dumps(value)))
        conn.commit()
        cur.close()
    except Exception as e:
        logger.error(f"Error setting bot value for key '{key}': {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()

def check_and_set_bot_lock(key):
    """Atomically checks if a lock is free and sets it. Returns True if lock was acquired."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Start transaction
        conn.autocommit = False
        
        # Lock the row for update
        cur.execute("SELECT value FROM bot_data WHERE key = %s FOR UPDATE", (key,))
        result = cur.fetchone()
        
        is_locked = result[0] if result else False
        
        if is_locked:
            # Lock is already held
            conn.commit() # Release lock
            cur.close()
            return False
        
        # Lock is free, acquire it
        cur.execute("""
            INSERT INTO bot_data (key, value)
            VALUES (%s, %s)
            ON CONFLICT (key)
            DO UPDATE SET value = EXCLUDED.value;
        """, (key, json.dumps(True)))
        
        conn.commit() # Commit transaction, release lock
        cur.close()
        return True
        
    except Exception as e:
        logger.error(f"Error in check_and_set_bot_lock for key '{key}': {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            conn.autocommit = True
            conn.close()

# --- User Data Functions (user_data) ---

def get_spam_data(user_id):
    """Gets spam info for a user."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT spam_blocked_until, spam_timestamps FROM user_data WHERE user_id = %s", (str(user_id),))
        result = cur.fetchone()
        cur.close()
        if result:
            return result[0], result[1] # (blocked_until, timestamps)
        return 0, []
    except Exception as e:
        logger.error(f"Error getting spam data for user {user_id}: {e}")
        return 0, []
    finally:
        if conn:
            conn.close()

def set_spam_data(user_id, blocked_until, timestamps):
    """Sets spam info for a user."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO user_data (user_id, spam_blocked_until, spam_timestamps)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id)
            DO UPDATE SET 
                spam_blocked_until = EXCLUDED.spam_blocked_until,
                spam_timestamps = EXCLUDED.spam_timestamps;
        """, (str(user_id), blocked_until, json.dumps(timestamps)))
        conn.commit()
        cur.close()
    except Exception as e:
        logger.error(f"Error setting spam data for user {user_id}: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()

def get_user_score(user_id):
    """Gets quiz score for a user."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT quiz_score FROM user_data WHERE user_id = %s", (str(user_id),))
        result = cur.fetchone()
        cur.close()
        if result:
            return result[0]
        return 0
    except Exception as e:
        logger.error(f"Error getting user score for {user_id}: {e}")
        return 0
    finally:
        if conn:
            conn.close()

def set_user_score(user_id, score):
    """Sets quiz score for a user."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO user_data (user_id, quiz_score)
            VALUES (%s, %s)
            ON CONFLICT (user_id)
            DO UPDATE SET quiz_score = EXCLUDED.quiz_score;
        """, (str(user_id), score))
        conn.commit()
        cur.close()
    except Exception as e:
        logger.error(f"Error setting user score for {user_id}: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()

async def update_message_count_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Increments message count for a user and updates their info."""
    user = update.effective_user
    user_id = str(user.id)
    username = user.username
    first_name = user.first_name
    conn = None
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO user_data (user_id, first_name, username, message_count)
            VALUES (%s, %s, %s, 1)
            ON CONFLICT (user_id)
            DO UPDATE SET
                message_count = user_data.message_count + 1,
                first_name = EXCLUDED.first_name,
                username = EXCLUDED.username;
        """, (user_id, first_name, username))
        conn.commit()
        cur.close()
    except Exception as e:
        logger.error(f"Error updating message count for user {user_id}: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()

# --- Chat Data Functions (chat_data) ---

def register_chat(update: Update):
    """Registers or updates a chat in the database."""
    chat = update.effective_chat
    if chat.type not in [constants.ChatType.GROUP, constants.ChatType.SUPERGROUP]:
        return
    
    chat_id = str(chat.id)
    title = chat.title
    conn = None
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO chat_data (chat_id, title, is_active)
            VALUES (%s, %s, TRUE)
            ON CONFLICT (chat_id)
            DO UPDATE SET
                title = EXCLUDED.title,
                is_active = TRUE;
        """, (chat_id, title))
        conn.commit()
        cur.close()
    except Exception as e:
        logger.error(f"Error registering chat {chat_id}: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()

def get_all_active_chat_ids():
    """Gets a list of all active chat IDs for broadcasting."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT chat_id FROM chat_data WHERE is_active = TRUE")
        results = cur.fetchall()
        cur.close()
        return [int(row[0]) for row in results]
    except Exception as e:
        logger.error(f"Error getting active chat IDs: {e}")
        return []
    finally:
        if conn:
            conn.close()

def deactivate_chat_in_db(chat_id):
    """Marks a chat as inactive if the bot is kicked/forbidden."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE chat_data SET is_active = FALSE WHERE chat_id = %s", (str(chat_id),))
        conn.commit()
        cur.close()
        logger.info(f"Deactivated chat {chat_id} in DB.")
    except Exception as e:
        logger.error(f"Error deactivating chat {chat_id}: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()


# ======================================================================
# --- 🏆 LEADERBOARD COMMANDS (Pehle leaderboard_manager mein thay) ---
# ======================================================================

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner-only command to broadcast a message to all active chats."""
    if not OWNER_ID or str(update.effective_user.id) != str(OWNER_ID):
        await update.message.reply_text("This is an owner-only command.")
        return

    message_text = update.message.text.split(' ', 1)
    if len(message_text) < 2:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    
    text_to_send = message_text[1]
    chat_ids = get_all_active_chat_ids()
    
    sent_count = 0
    failed_count = 0
    
    await update.message.reply_text(f"Starting broadcast to {len(chat_ids)} chats...")
    
    for chat_id in chat_ids:
        try:
            await context.bot.send_message(chat_id=chat_id, text=text_to_send, parse_mode=constants.ParseMode.HTML)
            sent_count += 1
        except (telegram.error.Forbidden, telegram.error.BadRequest) as e:
            logger.warning(f"Failed to send to {chat_id} (Forbidden/Bad Request): {e}. Deactivating chat.")
            deactivate_chat_in_db(chat_id)
            failed_count += 1
        except Exception as e:
            logger.error(f"Failed to send broadcast to {chat_id}: {e}")
            failed_count += 1
        await asyncio.sleep(0.2) # Avoid rate limits

    await update.message.reply_text(f"Broadcast complete.\nSent: {sent_count}\nFailed: {failed_count}")

def get_leaderboard_data(page=0, per_page=10):
    """Fetches paginated leaderboard data from the database."""
    offset = page * per_page
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # Fetch top users
        cur.execute("""
            SELECT first_name, message_count 
            FROM user_data 
            WHERE message_count > 0
            ORDER BY message_count DESC 
            LIMIT %s OFFSET %s
        """, (per_page, offset))
        top_users = cur.fetchall()
        
        # Fetch total user count
        cur.execute("SELECT COUNT(*) FROM user_data WHERE message_count > 0")
        total_users = cur.fetchone()[0]
        
        cur.close()
        return top_users, total_users
    except Exception as e:
        logger.error(f"Error fetching leaderboard data: {e}")
        return [], 0
    finally:
        if conn:
            conn.close()

async def ranking_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays the message count leaderboard."""
    await send_leaderboard_page(update, context, page=0)

async def send_leaderboard_page(update: Update, context: ContextTypes.DEFAULT_TYPE, page=0):
    """Helper function to send a specific page of the leaderboard."""
    per_page = 10
    top_users, total_users = get_leaderboard_data(page, per_page)
    
    if not top_users:
        text = "No one has chatted yet. Be the first!"
        await update.message.reply_text(text)
        return

    total_pages = (total_users + per_page - 1) // per_page
    
    text = "🏆 **Top Chatters** 🏆\n\n"
    rank_start = page * per_page
    
    for i, (first_name, count) in enumerate(top_users):
        rank = rank_start + i + 1
        name = html.escape(first_name or "Anonymous")
        emoji = "🥇" if rank == 1 else "🥈" if rank == 2 else "🥉" if rank == 3 else "🔹"
        text += f"{emoji} **{rank}.** {name} - {count} messages\n"
        
    text += f"\nPage {page + 1} of {total_pages}"

    buttons = []
    row = []
    if page > 0:
        row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"lb_page_{page - 1}"))
    if (page + 1) < total_pages:
        row.append(InlineKeyboardButton("Next ➡️", callback_data=f"lb_page_{page + 1}"))
    if row:
        buttons.append(row)

    reply_markup = InlineKeyboardMarkup(buttons) if buttons else None
    
    if isinstance(update, Update) and update.callback_query:
        # Edit message if it's a callback
        try:
            await update.callback_query.edit_message_text(text, parse_mode=constants.ParseMode.HTML, reply_markup=reply_markup)
        except telegram.error.BadRequest as e:
            if "message is not modified" in str(e):
                await update.callback_query.answer("You are already on this page.")
            else:
                logger.error(f"Error editing leaderboard message: {e}")
    else:
        # Send new message if it's a command
        await update.message.reply_text(text, parse_mode=constants.ParseMode.HTML, reply_markup=reply_markup)

async def leaderboard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the leaderboard pagination buttons."""
    query = update.callback_query
    await query.answer()
    
    try:
        page = int(query.data.split('_')[-1])
        await send_leaderboard_page(update, context, page=page)
    except Exception as e:
        logger.error(f"Error in leaderboard_callback: {e}")

async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays the profile for the user (or the replied-to user)."""
    user_to_check = update.effective_user
    if update.message.reply_to_message:
        user_to_check = update.message.reply_to_message.from_user
        
    user_id = str(user_to_check.id)
    first_name = html.escape(user_to_check.first_name)
    mention = user_to_check.mention_html()
    
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Get message count and quiz score in one query
        cur.execute("""
            SELECT message_count, quiz_score 
            FROM user_data 
            WHERE user_id = %s
        """, (user_id,))
        result = cur.fetchone()
        
        message_count = result[0] if result else 0
        quiz_score = result[1] if result else 0
        
        # Get rank
        cur.execute("""
            SELECT rank
            FROM (
                SELECT user_id, ROW_NUMBER() OVER (ORDER BY message_count DESC) as rank
                FROM user_data
                WHERE message_count > 0
            ) as ranked_users
            WHERE user_id = %s
        """, (user_id,))
        rank_result = cur.fetchone()
        rank = rank_result[0] if rank_result else "N/A"
        
        cur.close()
        
        text = (
            f"👤 **User Profile**\n\n"
            f"**Name:** {mention}\n"
            f"**User ID:** <code>{user_id}</code>\n\n"
            f"--- **Stats** ---\n"
            f"🏆 **Chat Rank:** {rank}\n"
            f"💬 **Messages:** {message_count}\n"
            f"🧠 **Quiz Score:** {quiz_score}"
        )
        
        await update.message.reply_text(text, parse_mode=constants.ParseMode.HTML)
        
    except Exception as e:
        logger.error(f"Error fetching profile for user {user_id}: {e}")
        await update.message.reply_text("Could not fetch profile.")
    finally:
        if conn:
            conn.close()

# ======================================================================
# --- 🎯 STANDARD COMMANDS (Updated) ---
# ======================================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_chat(update)
    bot = await context.bot.get_me()
    bot_name = escape_markdown(bot.first_name, version=2)
    user_name = escape_markdown(update.effective_user.first_name, version=2)
    start_text = (
        f"👋 *Hi {user_name}, I'm {bot_name}*\\!\n\n"
        f"I'm here to make this group fun with quizzes and rankings\\.\n\n"
        f"**What I can do:**\n"
        f"• 🏆 Track message rankings \\(/ranking\\)\n"
        f"• 👤 Check your stats with \\(/profile\\)\n"
        f"• 🧠 Run automatic quizzes as you chat\n"
        f"• 🏅 Track your personal quiz score \\(/myscore\\)\n"
        f"• 🖼️ Find images with \\/img `[query]`\n"
        f"• 🎨 Generate images with \\/gen `[prompt]`\n\n"
        f"Just start chatting to climb the ranks and trigger quizzes\\!"
    )
    if START_PHOTO_ID:
        try:
            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=START_PHOTO_ID,
                caption=start_text,
                parse_mode=constants.ParseMode.MARKDOWN_V2
            )
        except Exception as e:
            logger.error(f"Failed to send start photo: {e}.")
            await update.message.reply_text(start_text, parse_mode=constants.ParseMode.MARKDOWN_V2)
    else:
        await update.message.reply_text(start_text, parse_mode=constants.ParseMode.MARKDOWN_V2)

async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.new_chat_members: return
    register_chat(update)
    chat_id = update.effective_chat.id
    chat_name = html.escape(update.effective_chat.title or "this chat")

    video_id = None
    if WELCOME_VIDEO_URLS:
        video_index = get_bot_value(VIDEO_COUNTER_KEY, 0)
        video_id = WELCOME_VIDEO_URLS[video_index % len(WELCOME_VIDEO_URLS)]
        set_bot_value(VIDEO_COUNTER_KEY, video_index + 1)

    for member in update.message.new_chat_members:
        if member.is_bot: continue
        user_mention = member.mention_html()
        user_id = member.id
        welcome_message = (
            f"👋 <b>Welcome to {chat_name}</b>!\n\n"
            f"User: {user_mention}\n"
            f"Telegram ID: <code>{user_id}</code>\n\n"
            f"Chat and earn your spot on the leaderboard! 🏆"
        )
        try:
            if video_id:
                await context.bot.send_video(
                    chat_id=chat_id, video=video_id, 
                    caption=welcome_message, parse_mode=constants.ParseMode.HTML,
                )
            else:
                await context.bot.send_message(chat_id=chat_id, text=welcome_message, parse_mode=constants.ParseMode.HTML)
        except Exception as e:
            logger.error(f"Error during welcome message: {e}")
            await context.bot.send_message(chat_id=chat_id, text=welcome_message, parse_mode=constants.ParseMode.HTML)
                
async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot = await context.bot.get_me()
    bot_name = escape_markdown(bot.first_name, version=2)
    about_text = (
        f"👋 *About Me*\n\n"
        f"Hi, I'm {bot_name}\\!\n\n"
        f"I was created to help manage leaderboards, run quizzes, and have fun\\.\n\n"
        f"**Features:**\n"
        f"• Message rankings \\(/ranking\\)\n"
        f"• User profiles \\(/profile\\)\n"
        f"• Automatic quizzes (via buttons)\n"
        f"• Personal score tracking \\(/myscore\\)\n"
        f"• Image search \\(/img\\)\n"
        f"• AI Image generation \\(/gen\\)\n\n"
        f"•Owner: Gopu\n"
    )
    if OWNER_ID:
        about_text += f"You can contact my owner for support: [Owner](tg://user?id={OWNER_ID})\n"
    if ABOUT_PHOTO_ID:
        try:
            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=ABOUT_PHOTO_ID,
                caption=about_text,
                parse_mode=constants.ParseMode.MARKDOWN_V2
            )
        except Exception as e:
            logger.error(f"Failed to send about photo: {e}.")
            await update.message.reply_text(about_text, parse_mode=constants.ParseMode.MARKDOWN_V2)
    else:
        await update.message.reply_text(about_text, parse_mode=constants.ParseMode.MARKDOWN_V2)

async def get_id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("Please reply to a media file.")
        return
    replied_msg = update.message.reply_to_message
    file_id = (
        replied_msg.video.file_id if replied_msg.video else
        replied_msg.photo[-1].file_id if replied_msg.photo else
        replied_msg.audio.file_id if replied_msg.audio else
        replied_msg.document.file_id if replied_msg.document else
        replied_msg.sticker.file_id if replied_msg.sticker else
        None
    )
    file_type = (
        "Video" if replied_msg.video else "Photo" if replied_msg.photo else
        "Audio" if replied_msg.audio else "Document" if replied_msg.document else
        "Sticker" if replied_msg.sticker else "Unknown"
    )
    if file_id:
        await update.message.reply_text(f"<b>{file_type} File ID:</b> <code>{file_id}</code>", parse_mode=constants.ParseMode.HTML)
    else:
        await update.message.reply_text("Could not find a File ID.")

async def img_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not PEXELS_API_KEY:
        await update.message.reply_text("Image search is disabled.")
        return
    if not context.args:
        await update.message.reply_text("Example: `/img nature`")
        return
    query = " ".join(context.args)
    url = f"https://api.pexels.com/v1/search?query={query}&per_page=15"
    headers = {"Authorization": PEXELS_API_KEY}
    try:
        response = requests.get(url, headers=headers, timeout=5)
        response.raise_for_status()
        data = response.json()
        if not data.get('photos'):
            await update.message.reply_text(f"No images found for '{query}'.")
            return
        photo_url = random.choice(data['photos'])['src']['large']
        await update.message.reply_photo(photo_url, caption="Your Image")
    except Exception as e:
        logger.error(f"Pexels API error: {e}")
        await update.message.reply_text("Error with image search.")

async def gen_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Example: `/gen a cat in space`")
        return
    prompt = " ".join(context.args)
    sent_msg = await update.message.reply_text(f"🎨 Generating '{prompt}'...")
    try:
        post_url = "https://stablehorde.net/api/v2/generate/async"
        headers = {"apikey": STABLE_HORDE_API_KEY, "Client-Agent": "TelegramBot/1.0"}
        payload = {"prompt": prompt, "params": { "n": 1, "width": 512, "height": 512 }}
        post_response = requests.post(post_url, json=payload, headers=headers, timeout=10)
        post_response.raise_for_status()
        generation_id = post_response.json()['id']
        start_time = time.time()
        while time.time() - start_time < 120:
            await asyncio.sleep(5)
            check_url = f"https://stablehorde.net/api/v2/generate/check/{generation_id}"
            check_response = requests.get(check_url, timeout=5)
            check_data = check_response.json()
            if check_data.get('done', False):
                status_url = f"https://stablehorde.net/api/v2/generate/status/{generation_id}"
                status_response = requests.get(status_url, timeout=5)
                img_url = status_response.json()['generations'][0]['img']
                await sent_msg.delete()
                await update.message.reply_photo(img_url, caption=f"*Prompt:* {escape_markdown(prompt, version=2)}", parse_mode=constants.ParseMode.MARKDOWN_V2)
                return
        await sent_msg.edit_text("Generation timed out.")
    except Exception as e:
        logger.error(f"Stable Horde error: {e}")
        await sent_msg.edit_text(f"Sorry, an error occurred: {e}")

async def release_lock_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not OWNER_ID or str(update.effective_user.id) != str(OWNER_ID):
        await update.message.reply_text("This is an owner-only command.")
        return
    set_bot_value(LOCK_KEY, False)
    set_bot_value(LAST_GLOBAL_QUIZ_KEY, datetime.now(timezone.utc).timestamp()) 
    await update.message.reply_text("✅ Global quiz lock released, and global timer reset.")

async def myscore_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    score = get_user_score(user_id) # DB se
    user_name = html.escape(update.effective_user.first_name)
    await update.message.reply_text(
        f"🏆 <b>{user_name}'s Quiz Score</b>\n\n"
        f"You have answered <b>{score}</b> quizzes correctly!",
        parse_mode=constants.ParseMode.HTML
    )

# ======================================================================
# --- 🧠 QUIZ LOGIC (Button-based) ---
# ======================================================================

async def fetch_quiz_data_from_api():
    TRIVIA_API_URL = "https://opentdb.com/api.php?amount=1&type=multiple"
    try:
        response = requests.get(TRIVIA_API_URL, timeout=5)
        response.raise_for_status() 
        data = response.json()
        if data['response_code'] != 0 or not data['results']: return None
        q = data['results'][0]
        options = [html.unescape(requests.utils.unquote(ans)) for ans in q['incorrect_answers']]
        correct = html.unescape(requests.utils.unquote(q['correct_answer']))
        options.append(correct)
        random.shuffle(options)
        return {
            'question': html.unescape(requests.utils.unquote(q['question'])),
            'options': options,
            'correct_option_id': options.index(correct),
            'explanation': f"Correct Answer: {correct}"
        }
    except Exception as e:
        logger.error(f"Error fetching quiz data: {e}")
        return None

async def send_quiz_with_buttons(context: ContextTypes.DEFAULT_TYPE, chat_id, quiz_data, quiz_id):
    try:
        buttons = [[InlineKeyboardButton(html.unescape(opt), callback_data=f"quiz:{quiz_id}:{i}")] for i, opt in enumerate(quiz_data['options'])]
        keyboard = InlineKeyboardMarkup(buttons)
        sent_message = await context.bot.send_message(
            chat_id=chat_id,
            text=f"🧠 **Quiz Time!**\n\n{quiz_data['question']}",
            reply_markup=keyboard,
            parse_mode=constants.ParseMode.HTML 
        )
        return (chat_id, "Success", sent_message.message_id)
    except (telegram.error.Forbidden, telegram.error.BadRequest) as e:
        logger.warning(f"Failed to send to {chat_id}: {e}. Deactivating chat.")
        deactivate_chat_in_db(chat_id)
        return (chat_id, f"Failed_Deactivated: {e}", None)
    except Exception as e:
        logger.error(f"Failed to send quiz (button) to {chat_id}: {e}")
        return (chat_id, f"Failed_Error: {e}", None)

async def broadcast_quiz(context: ContextTypes.DEFAULT_TYPE):
    chat_ids = get_all_active_chat_ids()
    if not chat_ids:
        logger.warning("No active chats for broadcast.")
        return
    quiz_data = await fetch_quiz_data_from_api()
    if not quiz_data:
        logger.error("Failed to fetch quiz data, cancelling broadcast.")
        return
    
    # 1. Delete old quizzes
    old_quiz_messages = get_bot_value(LAST_QUIZ_MESSAGE_KEY, {})
    new_quiz_messages = {}
    delete_tasks = [context.bot.delete_message(chat_id=int(cid), message_id=mid) for cid, mid in old_quiz_messages.items()]
    if delete_tasks:
        logger.info(f"Attempting to delete {len(delete_tasks)} old quiz messages.")
        await asyncio.gather(*delete_tasks, return_exceptions=True) 
    
    # 2. Prep new quiz
    quiz_id = str(uuid.uuid4())
    open_quizzes = get_bot_value(OPEN_QUIZZES_KEY, {})
    open_quizzes[quiz_id] = {
        'correct': quiz_data['correct_option_id'],
        'explanation': quiz_data['explanation'],
        'answered_users': [] 
    }
    set_bot_value(OPEN_QUIZZES_KEY, open_quizzes)
    
    # 3. Broadcast new quiz
    tasks = [send_quiz_with_buttons(context, chat_id, quiz_data, quiz_id) for chat_id in chat_ids]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    successful_sends = 0
    for res in results:
        if isinstance(res, tuple) and res[1] == "Success":
            successful_sends += 1
            new_quiz_messages[str(res[0])] = res[2]
        elif isinstance(res, Exception):
            logger.error(f"Unexpected error during gather: {res}")
            
    set_bot_value(LAST_QUIZ_MESSAGE_KEY, new_quiz_messages)
    set_bot_value(LAST_GLOBAL_QUIZ_KEY, datetime.now(timezone.utc).timestamp())
    logger.info(f"Broadcast finished. Successful to {successful_sends}/{len(tasks)} chats. Quiz ID {quiz_id} is live.")

async def quiz_broadcast_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        logger.info("Starting global quiz broadcast job...")
        await broadcast_quiz(context) 
    except Exception as e:
        logger.error(f"Error during global quiz broadcast job: {e}")
        set_bot_value(LOCK_KEY, False)
        logger.info("Global quiz broadcast job FAILED. Lock released.")
    else:
        set_bot_value(LOCK_KEY, False)
        logger.info("Global quiz broadcast job finished. Lock released.")
        
async def handle_quiz_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    try:
        data = query.data.split(':') # "quiz:QUIZ_ID:SELECTED_INDEX"
        if len(data) != 3:
            await query.answer("Invalid quiz data.", show_alert=True)
            return

        quiz_id, selected_index = data[1], int(data[2])
        open_quizzes = get_bot_value(OPEN_QUIZZES_KEY, {})

        if quiz_id not in open_quizzes:
            await query.answer("This quiz has expired.", show_alert=True)
            try: await query.edit_message_reply_markup(reply_markup=None)
            except: pass
            return

        quiz_info = open_quizzes[quiz_id]
        if user_id in quiz_info['answered_users']:
            await query.answer("You have already answered this quiz!", show_alert=True)
            return
        
        quiz_info['answered_users'].append(user_id)
        set_bot_value(OPEN_QUIZZES_KEY, open_quizzes) 
        
        if selected_index == quiz_info['correct']:
            current_score = get_user_score(user_id)
            new_score = current_score + 1
            set_user_score(user_id, new_score) # DB update
            await query.answer(f"✅ Correct! Your score is now {new_score}.", show_alert=True)
        else:
            await query.answer(f"❌ Wrong! {quiz_info['explanation']}", show_alert=True)
    except Exception as e:
        logger.error(f"Error in handle_quiz_callback: {e}")
        try: await query.answer("An error occurred.", show_alert=True)
        except: pass

# ======================================================================
# --- 📨 CORE MESSAGE HANDLER (Quiz + Leaderboard) ---
# ======================================================================

async def send_quiz_after_n_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in [constants.ChatType.GROUP, constants.ChatType.SUPERGROUP] or not update.effective_user:
        return
    
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    # --- 1. Spam Protection ---
    current_time = time.time()
    blocked_until, message_timestamps = get_spam_data(user_id) 
    
    is_blocked = False
    if current_time < blocked_until:
        is_blocked = True
    else:
        message_timestamps = message_timestamps or []
        recent_timestamps = [t for t in message_timestamps if t > current_time - SPAM_TIME_WINDOW]
        recent_timestamps.append(current_time)
        
        if len(recent_timestamps) >= SPAM_MESSAGE_LIMIT:
            is_blocked = True
            new_blocked_until = current_time + SPAM_BLOCK_DURATION
            set_spam_data(user_id, new_blocked_until, [])
            logger.info(f"User {user_id} blocked for spamming.")
            try:
                mention = update.effective_user.mention_html()
                await update.message.reply_text(
                    f"{mention} <b>You are blocked for {int(SPAM_BLOCK_DURATION/60)} min for spamming!</b>\n"
                    f"Your messages will not be counted.",
                    parse_mode=constants.ParseMode.HTML
                )
            except Exception as e:
                logger.warning(f"Failed to send spam warning: {e}")
        else:
            set_spam_data(user_id, blocked_until, recent_timestamps)

    # --- 2. Update Message Count ---
    if not is_blocked:
        await update_message_count_db(update, context)
    
    # --- 3. Check for Quiz Trigger ---
    register_chat(update)
    if is_blocked:
        return
    
    last_quiz_time = get_bot_value(LAST_GLOBAL_QUIZ_KEY, 0)
    if current_time - last_quiz_time > GLOBAL_QUIZ_COOLDOWN:
        if not check_and_set_bot_lock(LOCK_KEY):
            return 
        
        context.job_queue.run_once(quiz_broadcast_job, 1, name="global_quiz_broadcast")
        logger.info(f"Global quiz cooldown over. Triggered by user {user_id} in chat {chat_id}. Scheduling broadcast job.")

# ======================================================================
# --- 🚀 MAIN EXECUTION ---
# ======================================================================

def main(): 
    if not TOKEN or not WEBHOOK_URL or not DATABASE_URL:
        logger.critical("FATAL ERROR: Environment variables missing (TOKEN, WEBHOOK_URL, or DATABASE_URL).")
        return
        
    # DB Setup on start
    setup_database()

    application = (
        Application.builder()
        .token(TOKEN)
        .concurrent_updates(True)
        .connect_timeout(10)   
        .read_timeout(15)      
        .write_timeout(15)     
        .http_version('1.1')
        .build()
    )
    
    application.add_error_handler(error_handler)
    
    # --- Add all handlers ---
    
    # Standard Commands
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("about", about_command))
    application.add_handler(CommandHandler("broadcast", broadcast_command)) # Local function

    # Leaderboard Commands
    application.add_handler(CommandHandler("ranking", ranking_command)) # Local function
    application.add_handler(CommandHandler("profile", profile_command)) # Local function
    application.add_handler(CommandHandler("prof", profile_command)) # Local function
    application.add_handler(CallbackQueryHandler(leaderboard_callback, pattern='^lb_')) # Local function
    
    # Quiz Score Command
    application.add_handler(CommandHandler("myscore", myscore_command))

    # Image Commands
    application.add_handler(CommandHandler("img", img_command))
    application.add_handler(CommandHandler("gen", gen_command))

    # Utility Commands
    application.add_handler(CommandHandler("get_id", get_id_command))
    application.add_handler(CommandHandler("release_lock", release_lock_command))

    # Message Handlers
    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
    application.add_handler(CallbackQueryHandler(handle_quiz_callback, pattern='^quiz:'))
    
    # Core Message handler (Quiz trigger + Message count)
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP),
            send_quiz_after_n_messages
        )
    )
    
    PORT = int(os.environ.get("PORT", "8000")) 
    
    logger.info("Starting FULL Bot (All-in-One main.py)...")
    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=TOKEN,
        webhook_url=f"{WEBHOOK_URL}/{TOKEN}"
    )

if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot shutdown gracefully.")
