# telegram_post_bot.py

import os
import logging
import sqlite3
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta

from telegram import Update, InputMediaPhoto
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    InlineQueryHandler,
    ContextTypes,
    filters
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
def fetch_html(url: str) -> str:
    resp = requests.get(url)
    resp.raise_for_status()
    return resp.text

def parse_article(html: str) -> dict:
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
    # Генерируем не больше 128 «новых» токенов – заметно быстрее
    result = styler(prompt, max_new_tokens=128)
    return result[0]['generated_text']

# === Публикация статьи ===
async def post_article(context: ContextTypes.DEFAULT_TYPE, url: str, chat_id: int = None):
    html = fetch_html(url)
    data = parse_article(html)
    combined = f"{data['title']}\n\n{data['lead']}\n\n{data['text']}"
    try:
        styled = generate_styled_post(combined)
    except Exception as e:
        logger.error(f"Rewriting failed: {e}")
        styled = combined

    target = chat_id or context.job.chat_id if hasattr(context, 'job') else context.application.bot_data.get('last_chat_id', ADMIN_CHAT_ID)

    if data['images']:
        await context.bot.send_photo(chat_id=target, photo=data['images'][0])
    await context.bot.send_message(chat_id=target, text=styled, parse_mode=ParseMode.HTML)

    db_conn.execute(
        'INSERT INTO posts (chat_id, date, url) VALUES (?,?,?)',
        (target, datetime.utcnow().isoformat(), url)
    )
    db_conn.commit()

# === Хэндлеры ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f">>> start handler called with chat_id={update.effective_chat.id}")
    await update.message.reply_text(
        "Привет! Отправь ссылку на статью, и я подготовлю пост в стиле КП-Кубань."
    )
    # Сохраним chat_id, чтобы было кому потом реджобу слать
    context.application.bot_data['last_chat_id'] = update.effective_chat.id

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    print(f">>> handle_link called with text: {text}")
    await post_article(context, text)

async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    await update.inline_query.answer(results, cache_time=0)

async def inline_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.chosen_inline_result.query
    await post_article(context, url, chat_id=update.chosen_inline_result.from_user.id)

async def digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    week_ago = datetime.utcnow() - timedelta(days=7)
    rows = db_conn.execute(
        'SELECT url FROM posts WHERE date>? ORDER BY date DESC LIMIT 5',
        (week_ago.isoformat(),)
    ).fetchall()
    text = "Топ-5 постов за неделю:\n" + '\n'.join(f"- {r[0]}" for r in rows)
    await update.message.reply_text(text)

async def auto_announce(context: ContextTypes.DEFAULT_TYPE):
    feed = feedparser.parse(RSS_FEED_URL)
    for entry in feed.entries[:5]:
        url = entry.link
        exists = db_conn.execute(
            'SELECT 1 FROM posts WHERE url=?', (url,)
        ).fetchone()
        if not exists:
            await post_article(context, url)

async def send_report(context: ContextTypes.DEFAULT_TYPE):
    week_ago = datetime.utcnow() - timedelta(days=7)
    count = db_conn.execute(
        'SELECT COUNT(*) FROM posts WHERE date>?', (week_ago.isoformat(),)
    ).fetchone()[0]
    msg = f"За прошлую неделю бот опубликовал {count} постов."
    await context.bot.send_message(ADMIN_CHAT_ID, msg)

# === Точка входа ===
if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Регистрируем хэндлеры
    app.add_handler(CommandHandler('start', start))
    app.add_handler(MessageHandler(filters.Entity('url'), handle_link))
    app.add_handler(InlineQueryHandler(inline_query))
    app.add_handler(CommandHandler('digest', digest))
    app.add_handler(InlineQueryHandler(inline_chosen))

    # Планируем автозапуски
    job_queue = app.job_queue
    job_queue.run_repeating(auto_announce, interval=1800, first=10)
    job_queue.run_repeating(send_report, interval=604800, first=0)

    print("🚀 Бот запущен!")
    app.run_polling()
