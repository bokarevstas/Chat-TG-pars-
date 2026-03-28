import asyncio
import base64
import json
import logging
import os
import re
from datetime import datetime, timezone, timedelta

import gspread
from google.oauth2.service_account import Credentials
from telethon import TelegramClient
from telethon.sessions import StringSession

# ============================================================
# КОНФИГУРАЦИЯ — все значения берутся из переменных окружения
# ============================================================
API_ID = int(os.environ.get('TG_API_ID', '0'))
API_HASH = os.environ.get('TG_API_HASH', '')
SESSION_STRING = os.environ.get('TG_SESSION', '')
SPREADSHEET_ID = os.environ.get('SPREADSHEET_ID', '')
GOOGLE_CREDENTIALS_BASE64 = os.environ.get('GOOGLE_CREDENTIALS_BASE64', '')

# Сколько минут назад смотреть сообщения (чуть больше интервала триггера)
LOOKBACK_MINUTES = int(os.environ.get('LOOKBACK_MINUTES', '35'))

# ============================================================
# ЛОГИРОВАНИЕ
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger(__name__)

# ============================================================
# GOOGLE SHEETS
# ============================================================
def get_spreadsheet():
    scopes = [
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive'
    ]
    creds_json = json.loads(base64.b64decode(GOOGLE_CREDENTIALS_BASE64).decode('utf-8'))
    creds = Credentials.from_service_account_info(creds_json, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(SPREADSHEET_ID)

def get_settings(ss):
    """Читает ключевые слова и флаг из листа Настройки."""
    try:
        sheet = ss.worksheet('Настройки')
        data = sheet.get_all_values()
        keywords_enabled = str(data[0][4]).upper() == 'TRUE' if data and len(data[0]) > 4 else False
        keywords = []
        for row in data[1:]:
            if len(row) > 3 and row[3].strip():
                keywords.append(row[3].strip())
        return keywords_enabled, keywords
    except Exception as e:
        log.error(f'Ошибка чтения настроек: {e}')
        return False, []

def get_allowed_chats(ss):
    """Читает список чатов из листа Каналы."""
    try:
        sheet = ss.worksheet('Каналы')
        data = sheet.get_all_values()
        chats = []
        for row in data[1:]:
            if row and row[0].strip():
                username = extract_username(row[0].strip())
                if username:
                    chats.append(username)
        return chats
    except Exception as e:
        log.error(f'Ошибка чтения каналов: {e}')
        return []

def get_existing_links(ss):
    """Возвращает set уже записанных ссылок для дедупликации."""
    try:
        sheet = ss.worksheet('Посты')
        data = sheet.get_all_values()
        return set(row[2] for row in data[1:] if len(row) > 2 and row[2])
    except Exception as e:
        log.error(f'Ошибка чтения постов: {e}')
        return set()

def write_posts(ss, posts):
    """Пакетная запись постов в лист Посты."""
    if not posts:
        return
    try:
        sheet = ss.worksheet('Посты')
        rows = [[
            p['date'].strftime('%Y-%m-%d %H:%M:%S'),
            p['chat_name'],
            p['link'],
            p['text']
        ] for p in posts]
        sheet.append_rows(rows, value_input_option='USER_ENTERED')
        log.info(f'Записано постов: {len(rows)}')
    except Exception as e:
        log.error(f'Ошибка записи постов: {e}')

def write_log(ss, level, message):
    """Пишет в лист Логи."""
    try:
        sheet = ss.worksheet('Логи')
        safe = str(message)
        if safe and safe[0] in '=+-@':
            safe = "'" + safe
        sheet.append_row(
            [datetime.now().strftime('%Y-%m-%d %H:%M:%S'), level, safe],
            value_input_option='USER_ENTERED'
        )
    except Exception as e:
        log.error(f'Ошибка записи лога: {e}')

# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================
def extract_username(raw):
    if not raw:
        return None
    m = re.match(r'(?:https?://)?t\.me/([a-zA-Z0-9_]+)', raw)
    if m:
        return m.group(1)
    if raw.startswith('@'):
        return raw[1:]
    if re.match(r'^[a-zA-Z0-9_]+$', raw):
        return raw
    if re.match(r'^-?\d+$', raw):
        return raw
    return None

def build_link(chat, msg_id):
    username = getattr(chat, 'username', None)
    if username:
        return f'https://t.me/{username}/{msg_id}'
    chat_id = str(chat.id)
    if chat_id.startswith('-100'):
        chat_id = chat_id[4:]
    return f'https://t.me/c/{chat_id}/{msg_id}'

def matches_keywords(text, keywords):
    if not text or not keywords:
        return False
    text_lower = text.lower()
    for kw in keywords:
        kw_lower = kw.lower().strip()
        if not kw_lower:
            continue
        if kw_lower.endswith('*'):
            if kw_lower[:-1] in text_lower:
                return True
            continue
        escaped = re.escape(kw_lower)
        if re.search(r'\b' + escaped + r'\b', text_lower):
            return True
        if len(kw_lower) > 4:
            root = escaped[:-2]
            suffixes = r'(ть|л|ла|ли|ло|ет|ешь|ем|ете|ут|ют|ит|ишь|им|ите|ат|ят|у|ю|а|я|е|и|ой|ей|ого|его|ому|ему|ом|ем|ых|их|ов|ами|ями)?'
            if re.search(r'\b' + root + suffixes + r'\b', text_lower):
                return True
    return False

# ============================================================
# ОСНОВНОЙ КОД
# ============================================================
async def main():
    log.info('Запуск прогона...')

    # Google Sheets
    try:
        ss = get_spreadsheet()
        log.info('Google Sheets подключён')
    except Exception as e:
        log.error(f'Ошибка Google Sheets: {e}')
        return

    # Настройки
    keywords_enabled, keywords = get_settings(ss)
    allowed_chats = get_allowed_chats(ss)
    existing_links = get_existing_links(ss)

    log.info(f'Чатов для парсинга: {len(allowed_chats)}')
    log.info(f'Ключевые слова: {"ВКЛ (" + str(len(keywords)) + " шт)" if keywords_enabled else "ВЫКЛ"}')

    if not allowed_chats:
        log.warning('Нет чатов в листе Каналы')
        write_log(ss, 'WARN', 'Нет чатов в листе Каналы')
        return

    # Telegram
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await client.start()
    log.info('Telegram подключён')

    since = datetime.now(timezone.utc) - timedelta(minutes=LOOKBACK_MINUTES)
    all_new_posts = []
    total_checked = 0
    total_new = 0
    total_saved = 0

    for chat_username in allowed_chats:
        try:
            # Получаем сообщения за последние LOOKBACK_MINUTES минут
            messages = []
            async for msg in client.iter_messages(chat_username, limit=50):
                if msg.date < since:
                    break
                messages.append(msg)

            chat = await client.get_entity(chat_username)
            chat_name = getattr(chat, 'title', None) or getattr(chat, 'username', None) or str(chat_username)

            new_msgs = []
            saved_msgs = []

            for msg in messages:
                text = msg.text or msg.message or ''
                if hasattr(msg, 'caption') and msg.caption:
                    text = msg.caption

                link = build_link(chat, msg.id)

                # Пропускаем уже записанные
                if link in existing_links:
                    continue

                new_msgs.append(msg)

                # Фильтр ключевых слов (только для постов с текстом)
                if keywords_enabled and keywords and text.strip():
                    if not matches_keywords(text, keywords):
                        continue

                date = msg.date.replace(tzinfo=None)
                saved_msgs.append({
                    'date': date,
                    'chat_name': chat_name,
                    'link': link,
                    'text': text
                })
                existing_links.add(link)

            total_checked += len(messages)
            total_new += len(new_msgs)
            total_saved += len(saved_msgs)
            all_new_posts.extend(saved_msgs)

            log.info(f'{chat_username} | за {LOOKBACK_MINUTES} мин: {len(messages)} | новых: {len(new_msgs)} | в таблицу: {len(saved_msgs)}')

        except Exception as e:
            log.error(f'{chat_username} | ОШИБКА: {e}')
            write_log(ss, 'ERROR', f'{chat_username} | {str(e)[:100]}')

        await asyncio.sleep(1)

    # Пакетная запись в таблицу
    write_posts(ss, all_new_posts)

    summary = f'ПРОГОН ЗАВЕРШЁН | чатов: {len(allowed_chats)} | проверено: {total_checked} | новых: {total_new} | записано: {total_saved}'
    log.info(summary)
    write_log(ss, 'INFO', summary)

    await client.disconnect()

if __name__ == '__main__':
    asyncio.run(main())
