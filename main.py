import asyncio
import base64
import json
import logging
import os
import re
import urllib.request
from datetime import datetime, timezone, timedelta

import gspread
from google.oauth2.service_account import Credentials
from telethon import TelegramClient
from telethon.sessions import StringSession

# ============================================================
# КОНФИГУРАЦИЯ
# ============================================================
API_ID = int(os.environ.get('TG_API_ID', '0'))
API_HASH = os.environ.get('TG_API_HASH', '')
SESSION_STRING = os.environ.get('TG_SESSION', '')
SPREADSHEET_ID = os.environ.get('SPREADSHEET_ID', '')
GOOGLE_CREDENTIALS_BASE64 = os.environ.get('GOOGLE_CREDENTIALS_BASE64', '')
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

def get_tg_settings(ss):
    """Читает токен бота и chat_id из листа Настройки."""
    try:
        sheet = ss.worksheet('Настройки')
        data = sheet.get_all_values()
        token = str(data[1][1]).strip() if len(data) > 1 and len(data[1]) > 1 else ''
        chats = []
        for row in data[2:]:
            if len(row) > 1 and str(row[1]).strip():
                chats.append(str(row[1]).strip())
        return token, chats
    except Exception as e:
        log.error(f'Ошибка чтения TG настроек: {e}')
        return '', []

def get_channels(ss):
    """
    Читает каналы из листа Каналы.
    Возвращает список словарей: {username, last_link, row_index}
    Колонка A — канал, B — последний пост, C — статус
    """
    try:
        sheet = ss.worksheet('Каналы')
        data = sheet.get_all_values()
        channels = []
        for i, row in enumerate(data[1:], start=2):
            if not row or not row[0].strip():
                continue
            raw = row[0].strip()
            username = extract_username(raw)
            if not username:
                continue
            last_link = row[1].strip() if len(row) > 1 else ''
            channels.append({
                'username': username,
                'last_link': last_link,
                'row': i
            })
        return channels
    except Exception as e:
        log.error(f'Ошибка чтения каналов: {e}')
        return []

def update_channel(ss, row, last_link, status):
    """Обновляет LastLink (col B) и Статус (col C) одним запросом."""
    try:
        sheet = ss.worksheet('Каналы')
        sheet.update(f'B{row}:C{row}', [[last_link, status]])
    except Exception as e:
        log.error(f'Ошибка обновления канала row={row}: {e}')

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
    """Пишет строку в лист Логи."""
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
# TELEGRAM ОТПРАВКА
# ============================================================
def send_to_telegram(posts, tg_token, tg_chats):
    """Отправляет посты в Telegram чаты/каналы через бота."""
    if not posts or not tg_token or not tg_chats:
        return
    for p in posts:
        text = f"📢 *{p['chat_name']}*\n\n{p['text']}\n\n🔗 {p['link']}"
        if len(text) > 4000:
            text = text[:4000] + '...'
        for chat_id in tg_chats:
            try:
                url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
                data = json.dumps({
                    'chat_id': chat_id,
                    'text': text,
                    'parse_mode': 'Markdown',
                    'disable_web_page_preview': False
                }).encode('utf-8')
                req = urllib.request.Request(
                    url, data=data,
                    headers={'Content-Type': 'application/json'}
                )
                urllib.request.urlopen(req, timeout=10)
                import time; time.sleep(0.3)
            except Exception as e:
                log.error(f'Ошибка отправки TG в {chat_id}: {e}')

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

def extract_post_id(link):
    m = re.search(r'/(\d+)$', link)
    return int(m.group(1)) if m else 0

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
    tg_token, tg_chats = get_tg_settings(ss)
    channels = get_channels(ss)

    log.info(f'Чатов: {len(channels)} | Ключи: {"ВКЛ (" + str(len(keywords)) + " шт)" if keywords_enabled else "ВЫКЛ"}')

    if not channels:
        log.warning('Нет каналов в листе Каналы')
        write_log(ss, 'WARN', 'Нет каналов в листе Каналы')
        return

    # Telegram клиент
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await client.start()
    log.info('Telegram подключён')

    all_new_posts = []
    total_new = 0
    total_saved = 0

    write_log(ss, 'INFO', f'ПРОГОН НАЧАТ | чатов: {len(channels)} | ключи: {"ВКЛ (" + str(len(keywords)) + " шт)" if keywords_enabled else "ВЫКЛ"}')

    for ch in channels:
        chat_username = ch['username']
        last_link = ch['last_link']
        last_post_id = extract_post_id(last_link) if last_link else 0
        row = ch['row']

        try:
            chat = await client.get_entity(chat_username)
            chat_name = getattr(chat, 'title', None) or getattr(chat, 'username', None) or str(chat_username)

            # Собираем новые сообщения по id > last_post_id
            # Дополнительно ограничиваем по времени если last_post_id == 0
            since = datetime.now(timezone.utc) - timedelta(minutes=LOOKBACK_MINUTES)
            messages = []

            async for msg in client.iter_messages(chat_username, limit=100):
                # Если есть last_post_id — берём всё новее него
                if last_post_id > 0:
                    if msg.id <= last_post_id:
                        break
                else:
                    # Первый запуск — берём за последние LOOKBACK_MINUTES минут
                    if msg.date < since:
                        break
                messages.append(msg)

            # Сортируем от старых к новым
            messages.sort(key=lambda m: m.id)

            new_msgs_count = len(messages)
            saved_msgs = []
            new_last_link = last_link  # по умолчанию не меняем

            for msg in messages:
                text = msg.text or msg.message or ''
                if hasattr(msg, 'caption') and msg.caption:
                    text = msg.caption

                link = build_link(chat, msg.id)
                date = msg.date.replace(tzinfo=None)

                # Обновляем last_link по каждому новому посту
                new_last_link = link

                # Фильтр ключевых слов только для постов с текстом
                if keywords_enabled and keywords and text.strip():
                    if not matches_keywords(text, keywords):
                        continue

                saved_msgs.append({
                    'date': date,
                    'chat_name': chat_name,
                    'link': link,
                    'text': text
                })

            total_new += new_msgs_count
            total_saved += len(saved_msgs)
            all_new_posts.extend(saved_msgs)

            # Обновляем LastLink и Статус в листе Каналы
            if new_msgs_count > 0:
                update_channel(ss, row, new_last_link, f'✅ Новых: {new_msgs_count} | Записано: {len(saved_msgs)}')
            else:
                update_channel(ss, row, last_link, '✅ Нет новых сообщений')

            log.info(f'{chat_username} | новых: {new_msgs_count} | в таблицу: {len(saved_msgs)} | lastId: {last_post_id or "пусто"}')

        except Exception as e:
            log.error(f'{chat_username} | ОШИБКА: {e}')
            update_channel(ss, row, last_link, f'❌ Ошибка: {str(e)[:50]}')
            write_log(ss, 'ERROR', f'{chat_username} | {str(e)[:100]}')

        await asyncio.sleep(1)

    # Пакетная запись в Посты
    write_posts(ss, all_new_posts)

    # Отправка в Telegram
    if all_new_posts and tg_token and tg_chats:
        log.info(f'Отправляю {len(all_new_posts)} постов в TG...')
        send_to_telegram(all_new_posts, tg_token, tg_chats)

    summary = f'ПРОГОН ЗАВЕРШЁН | чатов: {len(channels)} | новых: {total_new} | записано: {total_saved}'
    log.info(summary)
    write_log(ss, 'INFO', summary)

    await client.disconnect()

if __name__ == '__main__':
    asyncio.run(main())
