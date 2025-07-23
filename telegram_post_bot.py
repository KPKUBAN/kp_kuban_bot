import os
import logging
import sqlite3
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from telegram import InputMediaPhoto
from telegram.constants import ParseMode
from telegram.ext import (
    Updater, CommandHandler, MessageHandler,
    InlineQueryHandler, Filters, CallbackContext
)
from transformers import pipeline
import feedparser

# === Конфигурация ===
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
ADMIN_CHAT_ID = int(os.getenv('ADMIN_CHAT_ID', '0'))
RSS_FEED_URL = 'https://kuban.kp.ru/online/services/rss/'

# === Логи ===
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# === Модель Flan-T5 ===
styler = pipeline('text2text-generation', model='google/flan-t5-small')

# === Инициализация БД ===
def init_db():
    conn = sqlite3.connect('bot_data.db')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            date TEXT,
            url TEXT
        )
    ''')
    conn.commit()
    return conn

db_conn = init_db()

# === Парсинг статьи ===
def fetch_html(url):
    resp = requests.get(url)
    resp.raise_for_status()
    return resp.text

def parse_article(html):
    soup = BeautifulSoup(html, 'html.parser')
    title_tag = soup.find('h1', class_='article__title')
    title = title_tag.get_text(strip=True) if title_tag else ''
    lead_tag = soup.find('div', class_='article__lead')
    lead = lead_tag.get_text(strip=True) if lead_tag else ''
    text_div = soup.find('div', class_='article__text')
    paragraphs = text_div.find_all('p') if text_div else []
    text = '\n\n'.join(p.get_text(strip=True) for p in paragraphs)
    images = []
    if text_div:
        for img in text_div.find_all('img', src=True):
            src = img['src']
            if src.startswith('//'):
                src = 'https:' + src
            elif src.startswith('/'):
                src = 'https://kuban.kp.ru' + src
            images.append(src)
    return {'title': title, 'lead': lead, 'text': text, 'images': images}

# === Стилизация текста ===
def generate_styled_post(content: str) -> str:
    prompt = (
        "Перепиши в стиле Telegram-канала КП-Кубань: лаконично, "
        "с эмодзи, короткими абзацами. Текст: "
        + content
    )
    result = styler(prompt, max_length=512)
    return result[0]['generated_text']

# === Публикация статьи ===
def post_article(context, url, chat_id=None):
    html = fetch_html(url)
    data = parse_article(html)
    combined = f"{data['title']}\n\n{data['lead']}\n\n{data['text']}"
    try:
        styled = generate_styled_post(combined)
    except Exception as e:
        logger.error(f"Rewriting failed: {e}")
        styled = combined

    target = chat_id or context.effective_chat.id
    if data['images']:
        context.bot.send_photo(chat_id=target, photo=data['images'][0])
    context.bot.send_message(chat_id=target, text=styled, parse_mode=ParseMode.HTML)

    db_conn.execute(
        'INSERT INTO posts (chat_id,date,url) VALUES (?,?,?)',
        (target, datetime.utcnow().isoformat(), url)
    )
    db_conn.commit()

# === Хэндлеры ===
def start(update, context):
    update.message.reply_text(
        "Привет! Отправь ссылку на статью, и я подготовлю пост в стиле КП-Кубань."
    )

def handle_link(update, context):
    url = update.message.text.strip()
    post_article(context, url)

def inline_query(update, context):
    query = update.inline_query.query
    if not query.startswith('http'):
        return
    from telegram import InlineQueryResultArticle, InputTextMessageContent
    results = [
        InlineQueryResultArticle(
            id='1',
            title='Сгенерировать пост',
            input_message_content=InputTextMessageContent(query)
        )
    ]
    update.inline_query.answer(results, cache_time=0)

def inline_chosen(update, context):
    url = update.chosen_inline_result.query
    post_article(context, url, chat_id=update.chosen_inline_result.from_user.id)

def digest(update, context):
    week_ago = datetime.utcnow() - timedelta(days=7)
    rows = db_conn.execute(
        'SELECT url FROM posts WHERE date>? ORDER BY date DESC LIMIT 5',
        (week_ago.isoformat(),)
    ).fetchall()
    text = "Топ-5 постов за неделю:\n" + '\n'.join(f"- {r[0]}" for r in rows)
    update.message.reply_text(text)

def auto_announce(context):
    feed = feedparser.parse(RSS_FEED_URL)
    for entry in feed.entries[:5]:
        url = entry.link
        exists = db_conn.execute(
            'SELECT 1 FROM posts WHERE url=?', (url,)
        ).fetchone()
        if not exists:
            post_article(context, url)

def send_report(context):
    week_ago = datetime.utcnow() - timedelta(days=7)
    count = db_conn.execute(
        'SELECT COUNT(*) FROM posts WHERE date>?', (week_ago.isoformat(),)
    ).fetchone()[0]
    msg = f"За прошлую неделю бот опубликовал {count} постов."
    context.bot.send_message(ADMIN_CHAT_ID, msg)

if __name__ == '__main__':
    updater = Updater(TELEGRAM_TOKEN)
    dp = updater.dispatcher
    jq = updater.job_queue

    dp.add_handler(CommandHandler('start', start))
    dp.add_handler(MessageHandler(Filters.entity('url'), handle_link))
    dp.add_handler(InlineQueryHandler(inline_query))
    dp.add_handler(CommandHandler('digest', digest))
    dp.add_handler(InlineQueryHandler(inline_chosen, run_async=True))

    jq.run_repeating(auto_announce, interval=1800, first=10)
    jq.run_repeating(send_report, interval=604800, first=0)

    updater.start_polling()
    updater.idle()
