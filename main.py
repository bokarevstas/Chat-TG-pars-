import asyncio
import base64
import json
import logging
import os
import re
import urllib.request
from datetime import datetime, timezone, timedelta
import time
import gspread
from google.oauth2.service_account import Credentials
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, UsernameInvalidError, UsernameNotOccupiedError, ChannelPrivateError

API_ID = int(os.environ.get('TG_API_ID', '0'))
API_HASH = os.environ.get('TG_API_HASH', '')
SESSION_STRING = os.environ.get('TG_SESSION', '')
SPREADSHEET_ID = os.environ.get('SPREADSHEET_ID', '')
GOOGLE_CREDENTIALS_BASE64 = os.environ.get('GOOGLE_CREDENTIALS_BASE64', '')
LOOKBACK_MINUTES = int(os.environ.get('LOOKBACK_MINUTES', '35'))

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
log = logging.getLogger(__name__)

# Кеш entity: username -> объект чата
# Позволяет избежать повторных ResolveUsernameRequest на один и тот же канал
_entity_cache: dict = {}


def get_spreadsheet():
    scopes = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
    creds_json = json.loads(base64.b64decode(GOOGLE_CREDENTIALS_BASE64).decode('utf-8'))
    creds = Credentials.from_service_account_info(creds_json, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(SPREADSHEET_ID)


def get_settings(ss):
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
        log.error('Ошибка чтения настроек: ' + str(e))
        return False, []


def get_tg_settings(ss):
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
        log.error('Ошибка чтения TG настроек: ' + str(e))
        return '', []


def get_channels(ss):
    try:
        sheet = ss.worksheet('Каналы')
        data = sheet.get_all_values()
        channels = []
        for i, row in enumerate(data[1:], start=2):
            if not row or not row[0].strip():
                continue
            username = extract_username(row[0].strip())
            if not username:
                continue
            last_link = row[1].strip() if len(row) > 1 else ''
            channels.append({'username': username, 'last_link': last_link, 'row': i})
        return channels
    except Exception as e:
        log.error('Ошибка чтения каналов: ' + str(e))
        return []


def update_channel(ss, row, last_link, status):
    try:
        sheet = ss.worksheet('Каналы')
        sheet.update([[last_link, status]], 'B' + str(row) + ':C' + str(row))
    except Exception as e:
        log.error('Ошибка обновления канала row=' + str(row) + ': ' + str(e))


def write_posts(ss, posts):
    if not posts:
        return
    try:
        sheet = ss.worksheet('Посты')
        rows = [[
            p['date'].strftime('%Y-%m-%d %H:%M:%S'),
            p['chat_name'],
            p['author_name'],
            p['author_link'],
            p['link'],
            p['text']
        ] for p in posts]
        sheet.append_rows(rows, value_input_option='USER_ENTERED')
        log.info('Записано постов: ' + str(len(rows)))
    except Exception as e:
        log.error('Ошибка записи постов: ' + str(e))


def write_log(ss, level, message):
    try:
        sheet = ss.worksheet('Логи')
        safe = str(message)
        if safe and safe[0] in '=+-@':
            safe = "'" + safe
        sheet.append_row([datetime.now().strftime('%Y-%m-%d %H:%M:%S'), level, safe], value_input_option='USER_ENTERED')
    except Exception as e:
        log.error('Ошибка записи лога: ' + str(e))


def send_to_telegram(posts, tg_token, tg_chats):
    if not posts or not tg_token or not tg_chats:
        return
    for p in posts:
        parts = ['📢 ' + p['chat_name']]
        if p.get('author_name'):
            author_str = p['author_name']
            if p.get('author_link'):
                author_str += ' — ' + p['author_link']
            parts.append('👤 ' + author_str)
        parts.append('')
        parts.append(p['text'])
        parts.append('')
        parts.append('🔗 ' + p['link'])
        body = '\n'.join(parts)
        if len(body) > 4000:
            body = body[:4000] + '...'
        for chat_id in tg_chats:
            try:
                url = 'https://api.telegram.org/bot' + tg_token + '/sendMessage'
                data = json.dumps({'chat_id': chat_id, 'text': body, 'disable_web_page_preview': False}).encode('utf-8')
                req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
                urllib.request.urlopen(req, timeout=10)
                time.sleep(0.3)
            except Exception as e:
                log.error('Ошибка отправки TG в ' + str(chat_id) + ': ' + str(e) + ' | текст: ' + body[:200])
        time.sleep(0.3)


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
        return 'https://t.me/' + username + '/' + str(msg_id)
    chat_id = str(chat.id)
    if chat_id.startswith('-100'):
        chat_id = chat_id[4:]
    return 'https://t.me/c/' + chat_id + '/' + str(msg_id)


def get_author_info(msg):
    """Возвращает (имя_фамилия, ссылка_на_аккаунт)."""
    try:
        if not msg.sender:
            return '', ''
        sender = msg.sender
        first = getattr(sender, 'first_name', '') or ''
        last = getattr(sender, 'last_name', '') or ''
        username = getattr(sender, 'username', '') or ''
        full_name = (first + ' ' + last).strip()
        author_link = ('https://t.me/' + username) if username else ''
        return full_name, author_link
    except Exception:
        return '', ''


def matches_keywords(text, keywords):
    if not text or not keywords:
        return False
    text_lower = text.lower()
    for kw in keywords:
        kw_lower = kw.lower().strip().rstrip('*')
        if not kw_lower:
            continue
        if kw_lower in text_lower:
            return True
    return False


async def get_entity_safe(client, chat_username: str, max_retries: int = 3):
    """
    Резолвит entity с кешированием и обработкой FloodWaitError.

    - Если username уже в кеше — возвращает из кеша без запроса к TG.
    - При FloodWaitError ждёт указанное время (но не более 300 сек),
      затем повторяет. Если flood > 300 сек — поднимает исключение сразу.
    - При UsernameInvalidError / UsernameNotOccupiedError / ChannelPrivateError
      поднимает исключение без ретраев.
    """
    if chat_username in _entity_cache:
        return _entity_cache[chat_username]

    for attempt in range(1, max_retries + 1):
        try:
            entity = await client.get_entity(chat_username)
            _entity_cache[chat_username] = entity
            return entity
        except FloodWaitError as e:
            wait_sec = e.seconds
            if wait_sec > 300:
                # Слишком долго ждать — пропускаем канал
                raise RuntimeError(
                    f'FloodWait слишком большой ({wait_sec}s), канал пропущен'
                ) from e
            log.warning(
                f'FloodWait {wait_sec}s при резолве {chat_username} '
                f'(попытка {attempt}/{max_retries}), жду...'
            )
            await asyncio.sleep(wait_sec + 2)
        except (UsernameInvalidError, UsernameNotOccupiedError, ChannelPrivateError) as e:
            raise RuntimeError(f'Канал недоступен: {e}') from e

    raise RuntimeError(f'Не удалось получить entity для {chat_username} после {max_retries} попыток')


async def main():
    log.info('Запуск прогона...')
    try:
        ss = get_spreadsheet()
        log.info('Google Sheets подключён')
    except Exception as e:
        log.error('Ошибка Google Sheets: ' + str(e))
        return

    keywords_enabled, keywords = get_settings(ss)
    tg_token, tg_chats = get_tg_settings(ss)
    channels = get_channels(ss)
    log.info('Чатов: ' + str(len(channels)) + ' | Ключи: ' + ('ВКЛ (' + str(len(keywords)) + ' шт)' if keywords_enabled else 'ВЫКЛ'))

    if not channels:
        log.warning('Нет каналов в листе Каналы')
        write_log(ss, 'WARN', 'Нет каналов в листе Каналы')
        return

    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await client.start()
    log.info('Telegram подключён')

    all_new_posts = []
    total_new = 0
    total_saved = 0

    write_log(ss, 'INFO', 'ПРОГОН НАЧАТ | чатов: ' + str(len(channels)) + ' | ключи: ' + ('ВКЛ (' + str(len(keywords)) + ' шт)' if keywords_enabled else 'ВЫКЛ'))

    for ch in channels:
        chat_username = ch['username']
        last_link = ch['last_link']
        last_post_id = extract_post_id(last_link) if last_link else 0
        row = ch['row']

        try:
            # Резолвим entity один раз с кешированием — исключает повторные ResolveUsernameRequest
            chat = await get_entity_safe(client, chat_username)
            chat_name = getattr(chat, 'title', None) or getattr(chat, 'username', None) or str(chat_username)

            since = datetime.now(timezone.utc) - timedelta(minutes=LOOKBACK_MINUTES)
            messages = []

            # Передаём объект chat (не строку) — Telethon не делает повторный резолв
            async for msg in client.iter_messages(chat, limit=100):
                if last_post_id > 0:
                    if msg.id <= last_post_id:
                        break
                else:
                    if msg.date < since:
                        break
                messages.append(msg)

            messages.sort(key=lambda m: m.id)
            new_msgs_count = len(messages)
            saved_msgs = []
            new_last_link = last_link

            for msg in messages:
                # Пропускаем системные сообщения
                if msg.action is not None:
                    continue

                text = msg.text or msg.message or ''
                if hasattr(msg, 'caption') and msg.caption:
                    text = msg.caption

                # Текст в одну строку
                text = ' '.join(text.split())

                author_name, author_link = get_author_info(msg)
                link = build_link(chat, msg.id)
                date = msg.date.replace(tzinfo=None)
                new_last_link = link

                # Фильтр ключевых слов только для постов с текстом
                if keywords_enabled and keywords:
                    if text.strip():
                        if not matches_keywords(text, keywords):
                            continue
                    else:
                        # Пост без текста — пропускаем если фильтр включён
                        continue

                saved_msgs.append({
                    'date': date,
                    'chat_name': chat_name,
                    'author_name': author_name,
                    'author_link': author_link,
                    'link': link,
                    'text': text
                })

            total_new += new_msgs_count
            total_saved += len(saved_msgs)
            all_new_posts.extend(saved_msgs)

            if new_msgs_count > 0:
                update_channel(ss, row, new_last_link, '✅ Новых: ' + str(new_msgs_count) + ' | Записано: ' + str(len(saved_msgs)))
            else:
                update_channel(ss, row, last_link, '✅ Нет новых сообщений')

            log.info(chat_username + ' | новых: ' + str(new_msgs_count) + ' | в таблицу: ' + str(len(saved_msgs)) + ' | lastId: ' + (str(last_post_id) if last_post_id else 'пусто'))

        except FloodWaitError as e:
            # Глобальный FloodWait (не от резолва) — ждём и продолжаем
            wait_sec = e.seconds
            log.warning(f'{chat_username} | FloodWait {wait_sec}s, пропускаю канал')
            update_channel(ss, row, last_link, f'⏳ FloodWait {wait_sec}s')
            write_log(ss, 'WARN', f'{chat_username} | FloodWait {wait_sec}s')
            # Не ждём здесь — просто пропускаем канал, чтобы не блокировать весь прогон
        except Exception as e:
            log.error(chat_username + ' | ОШИБКА: ' + str(e))
            update_channel(ss, row, last_link, '❌ Ошибка: ' + str(e)[:50])
            write_log(ss, 'ERROR', chat_username + ' | ' + str(e)[:100])

        # Пауза между каналами для снижения нагрузки на TG API
        await asyncio.sleep(2)

    write_posts(ss, all_new_posts)

    if all_new_posts and tg_token and tg_chats:
        log.info('Отправляю ' + str(len(all_new_posts)) + ' постов в TG...')
        send_to_telegram(all_new_posts, tg_token, tg_chats)

    summary = 'ПРОГОН ЗАВЕРШЁН | чатов: ' + str(len(channels)) + ' | новых: ' + str(total_new) + ' | записано: ' + str(total_saved)
    log.info(summary)
    write_log(ss, 'INFO', summary)

    await client.disconnect()


if __name__ == '__main__':
    asyncio.run(main())
