# main.py (All-in-One Bot: No Chatting, Quiz Polls + Word Hustle)

import telegram
from telegram import Update, constants, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, 
    CommandHandler, 
    ContextTypes, 
    MessageHandler, 
    filters, 
    CallbackQueryHandler,
    PollAnswerHandler
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
# --- 💡 Import from new modules ---
from db_manager import (
    setup_database, get_db_connection, get_bot_value, set_bot_value, 
    check_and_set_bot_lock, get_spam_data, set_spam_data, get_user_score, 
    set_user_score, register_chat, get_all_active_chat_ids, deactivate_chat_in_db,
    get_leaderboard_data_quiz_only
)
from word_hustle import start_hustle_game, handle_hustle_guess, HUSTLE_GAME_KEY
# -----------------------------------

# --- ⚙️ Constants and Setup ---
GLOBAL_QUIZ_COOLDOWN = 600 # 10 minute (600s) global cooldown

# DB Keys
LOCK_KEY = 'global_quiz_lock' 
LAST_GLOBAL_QUIZ_KEY = 'last_global_quiz_time'
LAST_QUIZ_MESSAGE_KEY = 'last_quiz_poll_ids' # Storing Poll IDs
OPEN_QUIZZES_KEY = 'open_quizzes_polls' 
VIDEO_COUNTER_KEY = 'video_counter'

# Environment Variables
WELCOME_VIDEO_URLS = [
    "BAACAgUAAxkBAAIBxWkOFRIw0g1B_7xuSA4cyUE3ShSlAAJKGwAC_MFxVIx6wUDn1qKBNgQ",
    "BAACAgUAAxkBAAIByGkOFV746X7wbcPRCZTVy4iqtaC7AAKmIQACGV5QVJYHM5LZKdVANgQ",
]
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
WEBHOOK_URL = os.environ.get('RENDER_EXTERNAL_URL') 
OWNER_ID = os.environ.get('OWNER_ID')
PEXELS_API_KEY = os.environ.get('PEXELS_API_KEY')
STABLE_HORDE_API_KEY = os.environ.get('STABLE_HORDE_API_KEY', '0000000000')
START_PHOTO_ID = os.environ.get('START_PHOTO_ID') 
ABOUT_PHOTO_ID = os.environ.get('ABOUT_PHOTO_ID') 
DATABASE_URL = os.environ.get('DATABASE_URL')
SPAM_MESSAGE_LIMIT = 5 
SPAM_TIME_WINDOW = 5 
SPAM_BLOCK_DURATION = 1200


# --- Error Handler (Unchanged) ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)
    tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
    tb_string = "".join(tb_list)
    # ... (Error message sending logic remains) ...
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
# --- 🏆 LEADERBOARD COMMANDS (UPDATED: Quiz Score Only) ---
# ======================================================================

# /broadcast, /release_lock, /img, /gen, /get_id commands remain unchanged.

def get_leaderboard_data(page=0, per_page=10):
    # Ab sirf quiz score wala function use hoga
    return get_leaderboard_data_quiz_only(page, per_page)

async def ranking_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # message_count tracking is now removed.
    await send_leaderboard_page(update, context, page=0)

async def send_leaderboard_page(update: Update, context: ContextTypes.DEFAULT_TYPE, page=0):
    per_page = 10
    top_users, total_users = get_leaderboard_data(page, per_page)
    if not top_users:
        await update.message.reply_text("No one has earned a score yet.")
        return
    total_pages = (total_users + per_page - 1) // per_page
    # Title updated to reflect game focus
    text = "🧠 **Quiz & Hustle Score Leaderboard** 🏆\n\n"
    rank_start = page * per_page
    for i, (first_name, score) in enumerate(top_users):
        rank = rank_start + i + 1
        name = html.escape(first_name or "Anonymous")
        emoji = "🥇" if rank == 1 else "🥈" if rank == 2 else "🥉" if rank == 3 else "🔹"
        # Displaying score instead of message count
        text += f"{emoji} **{rank}.** {name} - {score} points\n"
    text += f"\nPage {page + 1} of {total_pages}"
    buttons = []
    row = []
    if page > 0: row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"lb_page_{page - 1}"))
    if (page + 1) < total_pages: row.append(InlineKeyboardButton("Next ➡️", callback_data=f"lb_page_{page + 1}"))
    if row: buttons.append(row)
    reply_markup = InlineKeyboardMarkup(buttons) if buttons else None
    if isinstance(update, Update) and update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, parse_mode=constants.ParseMode.HTML, reply_markup=reply_markup)
        except telegram.error.BadRequest as e:
            if "message is not modified" in str(e): await update.callback_query.answer("You are already on this page.")
            else: logger.error(f"Error editing leaderboard: {e}")
    else:
        await update.message.reply_text(text, parse_mode=constants.ParseMode.HTML, reply_markup=reply_markup)

async def leaderboard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data.split('_')[-1])
    await send_leaderboard_page(update, context, page=page)

async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_to_check = update.message.reply_to_message.from_user if update.message.reply_to_message else update.effective_user
    user_id = str(user_to_check.id)
    mention = user_to_check.mention_html()
    
    conn = get_db_connection()
    cur = conn.cursor()
    # Only querying quiz_score
    cur.execute("SELECT quiz_score FROM user_data WHERE user_id = %s", (user_id,))
    result = cur.fetchone()
    quiz_score = result[0] if result else 0
    
    # Getting rank based on quiz_score only
    cur.execute("SELECT rank FROM (SELECT user_id, ROW_NUMBER() OVER (ORDER BY quiz_score DESC) as rank FROM user_data WHERE quiz_score > 0) as ranked_users WHERE user_id = %s", (user_id,))
    rank_result = cur.fetchone()
    rank = rank_result[0] if rank_result else "N/A"
    cur.close()
    conn.close()
    
    # Text updated to remove message count
    text = f"👤 **User Profile**\n\n**Name:** {mention}\n**User ID:** <code>{user_id}</code>\n\n--- **Game Stats** ---\n🏆 **Score Rank:** {rank}\n🧠 **Total Score:** {quiz_score} points"
    await update.message.reply_text(text, parse_mode=constants.ParseMode.HTML)

# --- 💡 NEW: Owner-Only Timer Status Command ---
async def timer_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not OWNER_ID or str(update.effective_user.id) != str(OWNER_ID):
        await update.message.reply_text("❌ This is an owner-only command."); return
    
    current_time = time.time()
    last_quiz_time_ts = get_bot_value(LAST_GLOBAL_QUIZ_KEY, 0)
    is_locked = get_bot_value(LOCK_KEY, False)
    
    if last_quiz_time_ts == 0:
        last_quiz_time_str = "N/A (Never sent)"
        time_elapsed = GLOBAL_QUIZ_COOLDOWN # Max value
    else:
        last_quiz_dt = datetime.fromtimestamp(last_quiz_time_ts, timezone.utc)
        last_quiz_time_str = last_quiz_dt.strftime("%Y-%m-%d %H:%M:%S UTC")
        time_elapsed = current_time - last_quiz_time_ts
    
    time_remaining = max(0, GLOBAL_QUIZ_COOLDOWN - time_elapsed)
    
    # Status
    status = "🔴 ACTIVE" if is_locked else "🟢 FREE"
    cooldown_status = "✅ READY" if time_remaining <= 0 else f"⏳ {int(time_remaining)} seconds left"
    
    quiz_polls_count = len(get_bot_value(OPEN_QUIZZES_KEY, {}))
    hustle_games_count = len(get_bot_value(HUSTLE_GAME_KEY, {}))
    
    text = (
        f"**⌛ Global Timer Status (Owner Only)**\n\n"
        f"**Quiz Broadcast Lock:** `{status}`\n"
        f"**Last Broadcast:** `{last_quiz_time_str}`\n"
        f"**Cooldown ({GLOBAL_QUIZ_COOLDOWN}s):** `{cooldown_status}`\n\n"
        f"--- **Active Games** ---\n"
        f"**Open Quizzes (Polls):** `{quiz_polls_count}`\n"
        f"**Open Word Hustle:** `{hustle_games_count}`"
    )
    
    await update.message.reply_text(text, parse_mode=constants.ParseMode.MARKDOWN_V2)

# --- QUIZ LOGIC (Unchanged, from previous step) ---

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
        logger.error(f"Error fetching quiz data: {e}"); return None

async def send_quiz_poll(context: ContextTypes.DEFAULT_TYPE, chat_id, quiz_data):
    try:
        sent_message = await context.bot.send_poll(
            chat_id=chat_id,
            question=f"🧠 Quiz Time!\n\n{quiz_data['question']}",
            options=[html.unescape(opt) for opt in quiz_data['options']],
            is_anonymous=False, 
            type=constants.PollType.QUIZ,
            correct_option_id=quiz_data['correct_option_id'],
            explanation=f"✅ Correct: {quiz_data['explanation'].split(': ')[1]}",
            open_period=600 # 10 minutes
        )
        return sent_message.poll.id 
    except (telegram.error.Forbidden, telegram.error.BadRequest) as e:
        logger.warning(f"Failed to send poll to {chat_id}: {e}. Deactivating chat.")
        deactivate_chat_in_db(chat_id)
        raise
    except Exception as e:
        logger.error(f"Failed to send quiz poll to {chat_id}: {e}")
        raise

async def staggered_broadcast_job(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Starting STAGGERED broadcast job in background (Polls)...")
    try:
        chat_ids = get_all_active_chat_ids()
        if not chat_ids:
            logger.warning("No active chats for staggered broadcast."); return
        
        open_quizzes = get_bot_value(OPEN_QUIZZES_KEY, {})
        new_quiz_poll_ids = {}
        successful_sends = 0

        for chat_id in chat_ids:
            try:
                quiz_data = await fetch_quiz_data_from_api()
                if not quiz_data:
                    logger.error(f"Failed to fetch quiz data for chat {chat_id}. Skipping."); continue
                
                telegram_poll_id = await send_quiz_poll(context, chat_id, quiz_data)
                
                open_quizzes[telegram_poll_id] = {
                    'correct_option_id': quiz_data['correct_option_id'],
                    'answered_users': []
                }
                
                new_quiz_poll_ids[str(chat_id)] = telegram_poll_id 
                successful_sends += 1
                logger.info(f"Quiz Poll {telegram_poll_id} sent to chat {chat_id}.")

            except (telegram.error.Forbidden, telegram.error.BadRequest):
                logger.warning(f"Chat {chat_id} is deactivated. Skipping.")
            except Exception as e:
                logger.error(f"Unhandled error sending to chat {chat_id}: {e}")
            
            delay = random.randint(5, 15)
            await asyncio.sleep(delay)

        set_bot_value(OPEN_QUIZZES_KEY, open_quizzes)
        set_bot_value(LAST_QUIZ_MESSAGE_KEY, new_quiz_poll_ids)
        set_bot_value(LAST_GLOBAL_QUIZ_KEY, datetime.now(timezone.utc).timestamp())
        
        logger.info(f"Staggered broadcast FINISHED. Sent to {successful_sends}/{len(chat_ids)} chats. Global timer reset.")

    except Exception as e:
        logger.error(f"CRITICAL error in staggered_broadcast_job: {e}")
    finally:
        set_bot_value(LOCK_KEY, False)
        logger.info("Staggered broadcast job ended. Lock released.")

async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    poll_answer = update.poll_answer
    poll_id = poll_answer.poll_id
    user = poll_answer.user
    user_id = user.id
    
    if not poll_answer.option_ids: return

    chosen_option_index = poll_answer.option_ids[0]
    open_quizzes = get_bot_value(OPEN_QUIZZES_KEY, {})
    
    if poll_id not in open_quizzes: return
        
    quiz_info = open_quizzes[poll_id]
    
    if user_id in quiz_info['answered_users']: return

    if chosen_option_index == quiz_info['correct_option_id']:
        current_score = get_user_score(user_id)
        new_score = current_score + 1
        # Also update user's name/username
        set_user_score(user_id, new_score, first_name=user.first_name, username=user.username) 
        
        quiz_info['answered_users'].append(user_id)
        set_bot_value(OPEN_QUIZZES_KEY, open_quizzes)
        
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"✅ **Correct Answer!** You earned 1 point. Your total score is now **{new_score}**.",
                parse_mode=constants.ParseMode.MARKDOWN_V2
            )
        except telegram.error.Unauthorized:
            logger.info(f"Cannot DM user {user_id} score update.")
        except Exception as e:
            logger.error(f"Error sending DM score update: {e}")

# ======================================================================
# --- 📨 CORE MESSAGE HANDLER (MODIFIED: No Message Count Update) ---
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
    
    if current_time < blocked_until: is_blocked = True
    else:
        message_timestamps = [t for t in message_timestamps or [] if t > current_time - SPAM_TIME_WINDOW]
        message_timestamps.append(current_time)
        if len(message_timestamps) >= SPAM_MESSAGE_LIMIT:
            is_blocked = True
            set_spam_data(user_id, current_time + SPAM_BLOCK_DURATION, [])
            try:
                await update.message.reply_text(
                    f"{update.effective_user.mention_html()} <b>You are blocked for {int(SPAM_BLOCK_DURATION/60)} min for spamming!</b>",
                    parse_mode=constants.ParseMode.HTML
                )
            except: pass
        else: set_spam_data(user_id, blocked_until, message_timestamps)

    # --- 2. Check for Quiz Trigger (No Message Count update needed now) ---
    register_chat(update)
    if is_blocked: return
    
    last_quiz_time = get_bot_value(LAST_GLOBAL_QUIZ_KEY, 0)
    
    if current_time - last_quiz_time > GLOBAL_QUIZ_COOLDOWN:
        if not check_and_set_bot_lock(LOCK_KEY):
            logger.info("Quiz trigger attempted, but lock is already held."); return 
        
        logger.info(f"Global quiz cooldown over. Triggered by user {user_id}. ACQUIRING LOCK.")
        
        asyncio.create_task(staggered_broadcast_job(context))
        
        logger.info(f"Created background task for staggered broadcast. Handler is now free.")
    else:
        # Check for Word Hustle Guesses
        await handle_hustle_guess(update, context)


# ======================================================================
# --- 🚀 MAIN EXECUTION ---
# ======================================================================

def main(): 
    if not TOKEN or not WEBHOOK_URL or not DATABASE_URL:
        logger.critical("FATAL ERROR: Environment variables missing (TOKEN, WEBHOOK_URL, or DATABASE_URL).")
        return
        
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
    
    # --- Command Handlers ---
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("about", about_command))
    application.add_handler(CommandHandler("broadcast", broadcast_command))
    
    # Leaderboard Commands (Game Score only)
    application.add_handler(CommandHandler("ranking", ranking_command))
    application.add_handler(CommandHandler("profile", profile_command))
    application.add_handler(CommandHandler("prof", profile_command))
    application.add_handler(CallbackQueryHandler(leaderboard_callback, pattern='^lb_'))
    
    # Game Commands
    application.add_handler(CommandHandler("myscore", myscore_command))
    application.add_handler(CommandHandler("hustle", start_hustle_game)) # 💡 NEW GAME
    
    # Utility Commands
    application.add_handler(CommandHandler("img", img_command))
    application.add_handler(CommandHandler("gen", gen_command))
    application.add_handler(CommandHandler("get_id", get_id_command))
    application.add_handler(CommandHandler("release_lock", release_lock_command))
    application.add_handler(CommandHandler("timer_status", timer_status_command)) # 💡 NEW OWNER COMMAND

    # Message Handlers
    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
    application.add_handler(PollAnswerHandler(handle_poll_answer))
    
    # Core Message handler (Quiz trigger + Hustle guess check)
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP),
            send_quiz_after_n_messages
        )
    )
    
    PORT = int(os.environ.get("PORT", "8000")) 
    
    logger.info("Starting FULL Bot (Game-focused, NATIVE QUIZ POLLS, Word Hustle)...")
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
