# main.py (Monolithic Bot: Game-focused, Quiz Polls, Word Hustle, Fixed Errors)

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
import psycopg2 
from urllib.parse import urlparse 

# --- ⚙️ Constants and Setup ---
GLOBAL_QUIZ_COOLDOWN = 600 # 10 minute (600s) global cooldown

# DB Keys
LOCK_KEY = 'global_quiz_lock' 
LAST_GLOBAL_QUIZ_KEY = 'last_global_quiz_time'
LAST_QUIZ_MESSAGE_KEY = 'last_quiz_poll_ids' 
OPEN_QUIZZES_KEY = 'open_quizzes_polls' 
VIDEO_COUNTER_KEY = 'video_counter'
HUSTLE_GAME_KEY = 'current_hustle_game'
HUSTLE_TIMEOUT = 90 # 90 seconds to answer

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

# ======================================================================
# --- 💾 DATABASE FUNCTIONS ---
# ======================================================================

def get_db_connection():
    if not os.environ.get('DATABASE_URL'): raise ValueError("DATABASE_URL not set.")
    try:
        result = urlparse(os.environ.get('DATABASE_URL'))
        conn = psycopg2.connect(
            dbname=result.path[1:],
            user=result.username,
            password=result.password,
            host=result.hostname,
            port=result.port
        )
        return conn
    except Exception as e:
        logger.error(f"Failed to connect to database: {e}")
        raise

def setup_database():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS bot_data (key TEXT PRIMARY KEY, value JSONB);")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_data (
            user_id TEXT PRIMARY KEY, username TEXT, first_name TEXT,
            quiz_score INTEGER DEFAULT 0,
            spam_blocked_until FLOAT DEFAULT 0, spam_timestamps JSONB DEFAULT '[]'
        );""")
    cur.execute("CREATE TABLE IF NOT EXISTS chat_data (chat_id TEXT PRIMARY KEY, title TEXT, is_active BOOLEAN DEFAULT TRUE);")
    conn.commit()
    cur.close()
    conn.close()
    logger.info("Database tables verified/created successfully.")

def get_bot_value(key, default=None):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT value FROM bot_data WHERE key = %s", (key,))
    result = cur.fetchone()
    cur.close()
    conn.close()
    return result[0] if result else default

def set_bot_value(key, value):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO bot_data (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;", (key, json.dumps(value)))
    conn.commit()
    cur.close()
    conn.close()

def check_and_set_bot_lock(key):
    conn = get_db_connection()
    cur = conn.cursor()
    conn.autocommit = False
    try:
        cur.execute("SELECT value FROM bot_data WHERE key = %s FOR UPDATE", (key,))
        result = cur.fetchone()
        is_locked = result[0] if result else False
        if is_locked:
            conn.commit()
            return False
        cur.execute("INSERT INTO bot_data (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;", (key, json.dumps(True)))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Error in check_and_set_bot_lock: {e}")
        conn.rollback()
        return False
    finally:
        conn.autocommit = True
        cur.close()
        conn.close()

def get_spam_data(user_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT spam_blocked_until, spam_timestamps FROM user_data WHERE user_id = %s", (str(user_id),))
    result = cur.fetchone()
    cur.close()
    conn.close()
    return (result[0], result[1]) if result else (0, [])

def set_spam_data(user_id, blocked_until, timestamps):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO user_data (user_id, spam_blocked_until, spam_timestamps) VALUES (%s, %s, %s) ON CONFLICT (user_id) DO UPDATE SET spam_blocked_until = EXCLUDED.spam_blocked_until, spam_timestamps = EXCLUDED.spam_timestamps;", (str(user_id), blocked_until, json.dumps(timestamps)))
    conn.commit()
    cur.close()
    conn.close()

def get_user_score(user_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT quiz_score FROM user_data WHERE user_id = %s", (str(user_id),))
    result = cur.fetchone()
    cur.close()
    conn.close()
    return result[0] if result else 0

def set_user_score(user_id, score, first_name=None, username=None):
    conn = get_db_connection()
    cur = conn.cursor()
    if first_name and username:
        cur.execute("INSERT INTO user_data (user_id, quiz_score, first_name, username) VALUES (%s, %s, %s, %s) ON CONFLICT (user_id) DO UPDATE SET quiz_score = EXCLUDED.quiz_score, first_name = EXCLUDED.first_name, username = EXCLUDED.username;", (str(user_id), score, first_name, username))
    else:
        cur.execute("INSERT INTO user_data (user_id, quiz_score) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET quiz_score = EXCLUDED.quiz_score;", (str(user_id), score))
    conn.commit()
    cur.close()
    conn.close()

def get_leaderboard_data_quiz_only(page=0, per_page=10):
    offset = page * per_page
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT first_name, quiz_score FROM user_data WHERE quiz_score > 0 ORDER BY quiz_score DESC LIMIT %s OFFSET %s", (per_page, offset))
    top_users = cur.fetchall()
    cur.execute("SELECT COUNT(*) FROM user_data WHERE quiz_score > 0")
    total_users = cur.fetchone()[0]
    cur.close()
    conn.close()
    return top_users, total_users

def register_chat(update):
    chat = update.effective_chat
    if chat.type not in ['group', 'supergroup']: return
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO chat_data (chat_id, title, is_active) VALUES (%s, %s, TRUE) ON CONFLICT (chat_id) DO UPDATE SET title = EXCLUDED.title, is_active = TRUE;", (str(chat.id), chat.title))
    conn.commit()
    cur.close()
    conn.close()

def get_all_active_chat_ids():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT chat_id FROM chat_data WHERE is_active = TRUE")
    results = cur.fetchall()
    cur.close()
    conn.close()
    return [int(row[0]) for row in results]

def deactivate_chat_in_db(chat_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE chat_data SET is_active = FALSE WHERE chat_id = %s", (str(chat_id),))
    conn.commit()
    cur.close()
    conn.close()

# ======================================================================
# --- 🔠 WORD HUSTLE GAME LOGIC (FIXED: Using effective_message) ---
# ======================================================================

def scramble_word(word):
    """Word ko scramble karta hai."""
    word_list = list(word)
    if len(word) > 3:
        while True:
            random.shuffle(word_list)
            scrambled = "".join(word_list)
            if scrambled.lower() != word.lower():
                return scrambled
    else:
        random.shuffle(word_list)
        return "".join(word_list)

async def fetch_random_word():
    """API se ek random word fetch karta hai."""
    try:
        url = "https://random-word-api.herokuapp.com/word?number=1&lang=en"
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        word = response.json()[0].lower()
        if 5 <= len(word) <= 10:
            return word
        else:
            return await fetch_random_word()
    except Exception as e:
        logger.error(f"Error fetching random word: {e}")
        return None

async def start_hustle_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/hustle command handler."""
    chat_id = update.effective_chat.id
    
    active_games = get_bot_value(HUSTLE_GAME_KEY, {})
    if str(chat_id) in active_games and active_games[str(chat_id)].get('active'):
        await update.message.reply_text("⏳ **Word Hustle** already running! Guess the word or wait for it to end.", parse_mode=constants.ParseMode.MARKDOWN_V2)
        return

    original_word = await fetch_random_word()
    if not original_word:
        await update.message.reply_text("❌ Sorry, could not fetch a word right now. Try again later.")
        return

    scrambled_word = scramble_word(original_word)
    game_id = str(uuid.uuid4())
    start_time = time.time()
    
    active_games[str(chat_id)] = {
        'id': game_id,
        'word': original_word.lower(),
        'start_time': start_time,
        'chat_id': chat_id,
        'active': True
    }
    set_bot_value(HUSTLE_GAME_KEY, active_games)

    text = (
        f"🔥 **Word Hustle Challenge!** 🔥\n\n"
        f"Unscramble this word! You have **{HUSTLE_TIMEOUT} seconds**.\n\n"
        f"🔡 Scrambled Word: `{' '.join(list(scrambled_word.upper()))}`\n\n"
        f"Reply with your guess now!"
    )
    sent_msg = await update.message.reply_text(text, parse_mode=constants.ParseMode.MARKDOWN_V2)

    context.application.create_task(hustle_timeout_job(chat_id, game_id, original_word, sent_msg.message_id, context))

async def hustle_timeout_job(chat_id, game_id, original_word, message_id, context: ContextTypes.DEFAULT_TYPE):
    """Game end hone ke baad timer chala kar check karta hai."""
    await asyncio.sleep(HUSTLE_TIMEOUT)
    
    active_games = get_bot_value(HUSTLE_GAME_KEY, {})
    if str(chat_id) in active_games and active_games[str(chat_id)].get('id') == game_id and active_games[str(chat_id)].get('active'):
        
        active_games[str(chat_id)]['active'] = False
        set_bot_value(HUSTLE_GAME_KEY, active_games)
        
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=(
                    f"⏰ **Time's Up!** ⏰\n\n"
                    f"No one guessed the word in time.\n"
                    f"The word was: `{original_word.upper()}`"
                ),
                parse_mode=constants.ParseMode.MARKDOWN_V2
            )
        except Exception as e:
            logger.warning(f"Failed to edit hustle timeout message: {e}")

async def handle_hustle_guess(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User ke text messages ko check karta hai."""
    # FIX: Using effective_message to handle new/edited messages safely
    message = update.effective_message
    if not message or not message.text:
        return
        
    chat_id = message.chat.id
    user = update.effective_user
    guess = message.text.lower().strip()
    
    active_games = get_bot_value(HUSTLE_GAME_KEY, {})
    
    if str(chat_id) not in active_games or not active_games[str(chat_id)].get('active'):
        return
        
    game_info = active_games[str(chat_id)]
    
    if guess == game_info['word']:
        
        game_info['active'] = False
        set_bot_value(HUSTLE_GAME_KEY, active_games)
        
        current_score = get_user_score(user.id)
        new_score = current_score + 1
        set_user_score(user.id, new_score, first_name=user.first_name, username=user.username)
        
        mention = user.mention_html()
        await message.reply_text(
            f"🎉 <b>Correct!</b> {mention} unscrambled the word: <b>{game_info['word'].upper()}</b>\n\n"
            f"🏆 <b>Point earned!</b> Your total score is now <b>{new_score}</b>.",
            parse_mode=constants.ParseMode.HTML
        )
# ======================================================================
# --- 📝 QUIZ LOGIC ---
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
# --- 📝 COMMAND HANDLERS ---
# ======================================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)
    tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
    tb_string = "".join(tb_list)
    update_str = update.to_dict() if isinstance(update, Update) else str(update)
    message = (
        f"An exception was raised while handling an update\n"
        f"<pre>update = {html.escape(json.dumps(update_str, indent=2, ensure_ascii=False))</pre>\n\n"
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


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_chat(update)
    bot = await context.bot.get_me()
    bot_name = html.escape(bot.first_name)
    user_name = html.escape(update.effective_user.first_name)
    
    start_text = (
        f"👋 <b>Hi {user_name}, I'm {bot_name}</b>!\n\n"
        f"I'm a <b>Game-focused Bot</b> here to bring fun with Quizzes and Word Hustle challenges.\n\n"
        f"<b>What I can do:</b>\n"
        f"• 🏆 Track Quiz/Hustle scores (/ranking)\n"
        f"• 👤 Check your score with (/profile)\n"
        f"• 🧠 Trigger automatic Quiz Polls as you chat\n"
        f"• 🔠 Start a <b>Word Hustle</b> game with (/hustle)\n"
        f"• 🏅 Check your personal score (/myscore)\n\n"
        f"Just start chatting to potentially trigger a quiz, or use /hustle to start a challenge!"
    )
    
    photo_id = START_PHOTO_ID
    if photo_id:
        try:
            await context.bot.send_photo(chat_id=update.effective_chat.id, photo=photo_id, caption=start_text, parse_mode=constants.ParseMode.HTML)
        except:
            await update.message.reply_text(start_text, parse_mode=constants.ParseMode.HTML)
    else:
        await update.message.reply_text(start_text, parse_mode=constants.ParseMode.HTML)

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
        welcome_message = f"👋 <b>Welcome to {chat_name}</b>!\n\nUser: {member.mention_html()}\n\nStart playing quizzes and hustle to earn your spot on the leaderboard! 🏆"
        try:
            if video_id:
                await context.bot.send_video(chat_id=chat_id, video=video_id, caption=welcome_message, parse_mode=constants.ParseMode.HTML)
            else:
                await context.bot.send_message(chat_id=chat_id, text=welcome_message, parse_mode=constants.ParseMode.HTML)
        except Exception as e:
            logger.error(f"Error during welcome message: {e}")
            await context.bot.send_message(chat_id=chat_id, text=welcome_message, parse_mode=constants.ParseMode.HTML)

async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot = await context.bot.get_me()
    bot_name = html.escape(bot.first_name)
    about_text = (
        f"👋 <b>About Me</b>\n\nHi, I'm {bot_name}!\n\n"
        f"I was created for gaming and group engagement.\n\n"
        f"<b>Features:</b>\n"
        f"• Quiz/Hustle rankings (/ranking)\n"
        f"• User profiles (/profile)\n"
        f"• Automatic quizzes (via polls)\n"
        f"• Word Hustle game (/hustle)\n"
        f"• Personal score tracking (/myscore)\n"
        f"• Image search (/img)\n"
        f"• AI Image generation (/gen)\n\n"
        f"•Owner: Gopu\n"
    )
    if OWNER_ID: about_text += f"You can contact my owner for support: <a href='tg://user?id={OWNER_ID}'>Owner</a>\n"
    photo_id = ABOUT_PHOTO_ID
    if photo_id:
        try:
            await context.bot.send_photo(chat_id=update.effective_chat.id, photo=photo_id, caption=about_text, parse_mode=constants.ParseMode.HTML)
        except:
            await update.message.reply_text(about_text, parse_mode=constants.ParseMode.HTML)
    else:
        await update.message.reply_text(about_text, parse_mode=constants.ParseMode.HTML)

async def myscore_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    score = get_user_score(user_id)
    user_name = html.escape(update.effective_user.first_name)
    await update.message.reply_text(f"🏆 <b>{user_name}'s Total Game Score</b>\n\nYou have earned a total of <b>{score}</b> points!", parse_mode=constants.ParseMode.HTML)

async def get_id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message: await update.message.reply_text("Please reply to a media file."); return
    replied_msg = update.message.reply_to_message
    file_id = (
        replied_msg.video.file_id if replied_msg.video else
        replied_msg.photo[-1].file_id if replied_msg.photo else
        replied_msg.audio.file_id if replied_msg.audio else
        replied_msg.document.file_id if replied_msg.document else
        replied_msg.sticker.file_id if replied_msg.sticker else None
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
    if not PEXELS_API_KEY: await update.message.reply_text("Image search is disabled."); return
    if not context.args: await update.message.reply_text("Example: `/img nature`"); return
    query = " ".join(context.args)
    url = f"https://api.pexels.com/v1/search?query={query}&per_page=15"
    headers = {"Authorization": PEXELS_API_KEY}
    try:
        response = requests.get(url, headers=headers, timeout=5)
        response.raise_for_status()
        data = response.json()
        if not data.get('photos'): await update.message.reply_text(f"No images found for '{query}'."); return
        photo_url = random.choice(data['photos'])['src']['large']
        await update.message.reply_photo(photo_url, caption=f"Requested: {query}")
    except Exception as e:
        logger.error(f"Pexels API error: {e}"); await update.message.reply_text("Error with image search.")

async def gen_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not STABLE_HORDE_API_KEY or STABLE_HORDE_API_KEY == '0000000000': await update.message.reply_text("Image generation is disabled."); return
    if not context.args: await update.message.reply_text("Example: `/gen a cat in space`"); return
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
                # Use escape_markdown here as prompt might contain MkV2 reserved chars
                await update.message.reply_photo(img_url, caption=f"*Prompt:* {escape_markdown(prompt, version=2)}", parse_mode=constants.ParseMode.MARKDOWN_V2)
                return
        await sent_msg.edit_text("Generation timed out.")
    except Exception as e:
        logger.error(f"Stable Horde error: {e}"); await sent_msg.edit_text(f"Sorry, an error occurred: {e}")

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not OWNER_ID or str(update.effective_user.id) != str(OWNER_ID):
        await update.message.reply_text("This is an owner-only command.")
        return
    message_text = update.message.text.split(' ', 1)
    if len(message_text) < 2: await update.message.reply_text("Usage: /broadcast <message>"); return
    text_to_send = message_text[1]
    chat_ids = get_all_active_chat_ids()
    sent_count, failed_count = 0, 0
    await update.message.reply_text(f"Starting broadcast to {len(chat_ids)} chats...")
    for chat_id in chat_ids:
        try:
            await context.bot.send_message(chat_id=chat_id, text=text_to_send, parse_mode=constants.ParseMode.HTML)
            sent_count += 1
        except (telegram.error.Forbidden, telegram.error.BadRequest) as e:
            deactivate_chat_in_db(chat_id)
            failed_count += 1
        except Exception as e:
            logger.error(f"Failed to send broadcast to {chat_id}: {e}")
            failed_count += 1
        await asyncio.sleep(0.2)
    await update.message.reply_text(f"Broadcast complete.\nSent: {sent_count}\nFailed: {failed_count}")

async def release_lock_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not OWNER_ID or str(update.effective_user.id) != str(OWNER_ID):
        await update.message.reply_text("This is an owner-only command."); return
    set_bot_value(LOCK_KEY, False)
    set_bot_value(LAST_GLOBAL_QUIZ_KEY, datetime.now(timezone.utc).timestamp()) 
    await update.message.reply_text("✅ Global quiz lock released, and global timer reset.")

def get_leaderboard_data(page=0, per_page=10):
    return get_leaderboard_data_quiz_only(page, per_page)

async def ranking_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_leaderboard_page(update, context, page=0)

async def send_leaderboard_page(update: Update, context: ContextTypes.DEFAULT_TYPE, page=0):
    per_page = 10
    top_users, total_users = get_leaderboard_data(page, per_page)
    if not top_users:
        await update.message.reply_text("No one has earned a score yet.")
        return
    total_pages = (total_users + per_page - 1) // per_page
    text = "🧠 <b>Quiz & Hustle Score Leaderboard</b> 🏆\n\n"
    rank_start = page * per_page
    for i, (first_name, score) in enumerate(top_users):
        rank = rank_start + i + 1
        name = html.escape(first_name or "Anonymous")
        emoji = "🥇" if rank == 1 else "🥈" if rank == 2 else "🥉" if rank == 3 else "🔹"
        text += f"{emoji} <b>{rank}.</b> {name} - {score} points\n"
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
    cur.execute("SELECT quiz_score FROM user_data WHERE user_id = %s", (user_id,))
    result = cur.fetchone()
    quiz_score = result[0] if result else 0
    
    cur.execute("SELECT rank FROM (SELECT user_id, ROW_NUMBER() OVER (ORDER BY quiz_score DESC) as rank FROM user_data WHERE quiz_score > 0) as ranked_users WHERE user_id = %s", (user_id,))
    rank_result = cur.fetchone()
    rank = rank_result[0] if rank_result else "N/A"
    cur.close()
    conn.close()
    
    text = f"👤 <b>User Profile</b>\n\n<b>Name:</b> {mention}\n<b>User ID:</b> <code>{user_id}</code>\n\n--- <b>Game Stats</b> ---\n🏆 <b>Score Rank:</b> {rank}\n🧠 <b>Total Score:</b> {quiz_score} points"
    await update.message.reply_text(text, parse_mode=constants.ParseMode.HTML)

async def timer_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not OWNER_ID or str(update.effective_user.id) != str(OWNER_ID):
        await update.message.reply_text("❌ This is an owner-only command."); return
    
    current_time = time.time()
    last_quiz_time_ts = get_bot_value(LAST_GLOBAL_QUIZ_KEY, 0)
    is_locked = get_bot_value(LOCK_KEY, False)
    
    if last_quiz_time_ts == 0:
        last_quiz_time_str = "N/A (Never sent)"
        time_elapsed = GLOBAL_QUIZ_COOLDOWN 
    else:
        last_quiz_dt = datetime.fromtimestamp(last_quiz_time_ts, timezone.utc)
        last_quiz_time_str = last_quiz_dt.strftime("%Y-%m-%d %H:%M:%S UTC")
        time_elapsed = current_time - last_quiz_time_ts
    
    time_remaining = max(0, GLOBAL_QUIZ_COOLDOWN - time_elapsed)
    
    status = "🔴 ACTIVE (Quiz broadcast in progress)" if is_locked else "🟢 FREE (Ready to broadcast)"
    cooldown_status = "✅ READY" if time_remaining <= 0 else f"⏳ {int(time_remaining)} seconds left"
    
    quiz_polls_count = len(get_bot_value(OPEN_QUIZZES_KEY, {}))
    active_games = get_bot_value(HUSTLE_GAME_KEY, {})
    active_hustle_games_count = sum(1 for game in active_games.values() if game.get('active'))
    
    text = (
        f"**⌛ Global Timer Status (Owner Only)**\n\n"
        f"**Quiz Broadcast Lock:** `{status}`\n"
        f"**Last Broadcast:** `{last_quiz_time_str}`\n"
        f"**Cooldown ({GLOBAL_QUIZ_COOLDOWN}s):** `{cooldown_status}`\n\n"
        f"\\-\\- **Active Games** \\-\\- \n" 
        f"**Open Quizzes (Polls):** `{quiz_polls_count}`\n"
        f"**Open Word Hustle:** `{active_hustle_games_count}`"
    )
    
    await update.message.reply_text(text, parse_mode=constants.ParseMode.MARKDOWN_V2)

# ======================================================================
# --- 📨 CORE MESSAGE HANDLER (FIXED: Filtered for NEW messages) ---
# ======================================================================

async def send_quiz_after_n_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # This handler is now guaranteed to only run for NEW messages that have text 
    # and are not commands, thanks to the filter update in main().
    
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

    # --- 2. Registration and Quiz/Hustle Check ---
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
        # handle_hustle_guess is now robust against NoneType error
        await handle_hustle_guess(update, context)


# ======================================================================
# --- 🚀 MAIN EXECUTION (FIXED: Filter updated) ---
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
    application.add_handler(CommandHandler("ranking", ranking_command))
    application.add_handler(CommandHandler("profile", profile_command))
    application.add_handler(CommandHandler("prof", profile_command))
    application.add_handler(CallbackQueryHandler(leaderboard_callback, pattern='^lb_'))
    application.add_handler(CommandHandler("myscore", myscore_command))
    application.add_handler(CommandHandler("hustle", start_hustle_game)) 
    application.add_handler(CommandHandler("img", img_command))
    application.add_handler(CommandHandler("gen", gen_command))
    application.add_handler(CommandHandler("get_id", get_id_command))
    application.add_handler(CommandHandler("release_lock", release_lock_command))
    application.add_handler(CommandHandler("timer_status", timer_status_command)) 

    # Message Handlers
    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
    application.add_handler(PollAnswerHandler(handle_poll_answer))
    
    # Core Message handler (Quiz trigger + Hustle guess check)
    # FIX: Added `& ~filters.UpdateType.EDITED_MESSAGE` to prevent processing edited messages.
    application.add_handler(
        MessageHandler(
            filters.TEXT 
            & ~filters.COMMAND 
            & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP)
            & ~filters.UpdateType.EDITED_MESSAGE, 
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
