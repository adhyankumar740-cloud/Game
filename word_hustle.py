# word_hustle.py

import requests
import random
import asyncio
import time
import html
import uuid
import logging
from telegram import Update, constants
from telegram.ext import ContextTypes
from db_manager import get_bot_value, set_bot_value, set_user_score, get_user_score

logger = logging.getLogger(__name__)

HUSTLE_GAME_KEY = 'current_hustle_game'
HUSTLE_TIMEOUT = 90 # 90 seconds to answer

def scramble_word(word):
    """Word ko scramble karta hai."""
    word_list = list(word)
    # Ensure the scrambled word is not the same as the original (for words > 3 chars)
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
        # Request for a random word of reasonable length (e.g., 5-10 letters)
        url = "https://random-word-api.herokuapp.com/word?number=1&lang=en"
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        word = response.json()[0].lower()
        if 5 <= len(word) <= 10:
            return word
        else:
            # Agar word chota ya bada ho toh retry
            return await fetch_random_word()
    except Exception as e:
        logger.error(f"Error fetching random word: {e}")
        return None

async def start_hustle_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/hustle command handler."""
    chat_id = update.effective_chat.id
    
    # Check for active game in this chat
    active_games = get_bot_value(HUSTLE_GAME_KEY, {})
    if str(chat_id) in active_games:
        await update.message.reply_text("⏳ **Word Hustle** already running! Guess the word or wait for it to end.", parse_mode=constants.ParseMode.MARKDOWN_V2)
        return

    original_word = await fetch_random_word()
    if not original_word:
        await update.message.reply_text("❌ Sorry, could not fetch a word right now. Try again later.")
        return

    scrambled_word = scramble_word(original_word)
    game_id = str(uuid.uuid4())
    start_time = time.time()
    
    # Game state store karo
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

    # Background task for game timeout
    context.application.create_task(hustle_timeout_job(chat_id, game_id, original_word, sent_msg.message_id, context))

async def hustle_timeout_job(chat_id, game_id, original_word, message_id, context: ContextTypes.DEFAULT_TYPE):
    """Game end hone ke baad timer chala kar check karta hai."""
    await asyncio.sleep(HUSTLE_TIMEOUT)
    
    active_games = get_bot_value(HUSTLE_GAME_KEY, {})
    if str(chat_id) in active_games and active_games[str(chat_id)].get('id') == game_id and active_games[str(chat_id)].get('active'):
        
        # Game ko khatam karo
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
    chat_id = update.effective_chat.id
    user = update.effective_user
    guess = update.message.text.lower().strip()
    
    active_games = get_bot_value(HUSTLE_GAME_KEY, {})
    
    if str(chat_id) not in active_games or not active_games[str(chat_id)].get('active'):
        return # Koi active game nahi hai
        
    game_info = active_games[str(chat_id)]
    
    if guess == game_info['word']:
        # Sahi guess!
        
        # Game ko deactivate karo
        game_info['active'] = False
        set_bot_value(HUSTLE_GAME_KEY, active_games)
        
        # Score update karo
        current_score = get_user_score(user.id)
        new_score = current_score + 1
        set_user_score(user.id, new_score, first_name=user.first_name, username=user.username)
        
        # Confirmation message
        mention = user.mention_html()
        await update.message.reply_text(
            f"🎉 **Correct!** {mention} unscrambled the word: **{game_info['word'].upper()}**\n\n"
            f"🏆 **Point earned!** Your total score is now **{new_score}**.",
            parse_mode=constants.ParseMode.HTML
        )
        
        # Original message ko edit karke game end dikhao
        try:
            # Find the original message ID (if available, using a placeholder method)
            # Since we don't store it here easily, we rely on the reply message to announce the win.
            pass
        except Exception:
            pass
