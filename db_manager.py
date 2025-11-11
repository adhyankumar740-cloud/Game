# db_manager.py

import psycopg2 
import os
import logging
import json
from urllib.parse import urlparse 

logger = logging.getLogger(__name__)

# --- DB Utility Functions ---

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

# --- Bot Data (Global Key/Value) ---

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

# --- User Data (Score/Spam) ---

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
    # Updated to ensure user record exists or is created on score update
    if first_name and username:
        cur.execute("INSERT INTO user_data (user_id, quiz_score, first_name, username) VALUES (%s, %s, %s, %s) ON CONFLICT (user_id) DO UPDATE SET quiz_score = EXCLUDED.quiz_score, first_name = EXCLUDED.first_name, username = EXCLUDED.username;", (str(user_id), score, first_name, username))
    else:
        cur.execute("INSERT INTO user_data (user_id, quiz_score) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET quiz_score = EXCLUDED.quiz_score;", (str(user_id), score))
    conn.commit()
    cur.close()
    conn.close()

# --- Leaderboard Data (NEW: Quiz Score only) ---

def get_leaderboard_data_quiz_only(page=0, per_page=10):
    offset = page * per_page
    conn = get_db_connection()
    cur = conn.cursor()
    # Only sort by quiz_score
    cur.execute("SELECT first_name, quiz_score FROM user_data WHERE quiz_score > 0 ORDER BY quiz_score DESC LIMIT %s OFFSET %s", (per_page, offset))
    top_users = cur.fetchall()
    cur.execute("SELECT COUNT(*) FROM user_data WHERE quiz_score > 0")
    total_users = cur.fetchone()[0]
    cur.close()
    conn.close()
    return top_users, total_users

# --- Chat Data ---

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
