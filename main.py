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
from telethon.errors import (
    FloodWaitError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
    ChannelPrivateError,
    ChatAdminRequiredError,
)
from telethon.tl.types import InputPeerChannel

# ══════════════════════════════════════════════════════
#  Конфигурация из переменных окружения
# ══════════════════════════════════════════════════════

API_ID = int(os.environ.get('TG_API_ID', '0'))
API_HASH = os.environ.get('TG_API_HASH', '')
SESSION_STRING = os.environ.get('TG_SESSION', '')
SPREADSHEET_ID = os.environ.get('SPREADSHEET_ID', '')       # уникален для каждой конфигурации
GOOGLE_CREDENTIALS_BASE64 = os.environ.get('GOOGLE_CREDENTIALS_BASE64', '')
LOOKBACK_MINUTES = int(os.environ.get('LOOKBACK_MINUTES', '35'))

MAX_FLOOD_WAIT_SEC = 300  # FloodWait больше этого — канал пропускается

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger(__name__)

_entity_cache: dict = {}


# ══════════════════════════════════════════════════════
#  Парсинг адреса канала
# ══════════════════════════════════════════════════════

def parse_channel_address(raw: str) -> dict:
    """
    Поддерживаемые форматы колонки A листа «Каналы»:
      @username
      username
      https://t.me/username
      https://t.me/c/1924271762/3178  ← приватная супергруппа
      -1001924271762                   ← числовой ID
    """
    raw = raw.strip()
    if not raw:
        return {}

    m = re.match(r'(?:https?://)?t\.me/c/(\d+)(?:/\d+)?', raw)
    if m:
        return {'type': 'private', 'username': None, 'channel_id': int(m.group(1))}

    if re.match(r'^-?\d+$', raw):
        cid = abs(int(raw))
        s = str(cid)
        if s.startswith('100'):
            cid = int(s[3:])
        return {'type': 'private', 'username': None, 'channel_id': cid}

    m = re.match(r'(?:https?://)?t\.me/([a-zA-Z0-9_]+)', raw)
    if m:
        return {'type': 'username', 'username': m.group(1), 'channel_id': None}

    if raw.startswith('@'):
        return {'type': 'username', 'username': raw[1:], 'channel_id': None}

    if re.match(r'^[a-zA-Z0-9_]+$', raw):
        return {'type': 'username', 'username': raw, 'channel_id': None}

    return {}


# ══════════════════════════════════════════════════════
#  Google Sheets
# ══════════════════════════════════════════════════════

def get_spreadsheet():
    scopes = [
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive',
    ]
    creds_json = json.loads(base64.b64decode(GOOGLE_CREDENTIALS_BASE64).decode('utf-8'))
    creds = Credentials.from_service_account_info(creds_json, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(SPREADSHEET_ID)


def get_settings(ss):
    """
    Лист «Настройки»:
      A2  = «TG-бот»  B2 = токен бота
      A3  = «Чаты»    B3 = метка (игнорируем)
      A4+ = chat_id для отправки (B4+ — не используется)
      D   = ключевые слова (строки 2+)
      F   = негативные слова (строки 2+)
      H1  = чекбокс включения фильтра (TRUE/FALSE)

    Возвращает: keywords_enabled, keywords, negatives, tg_token, tg_chats
    """
    try:
        sheet = ss.worksheet('Настройки')
        data = sheet.get_all_values()

        # H1 — чекбокс (индекс 7)
        keywords_enabled = str(data[0][7]).upper() == 'TRUE' if data and len(data[0]) > 7 else False

        # B2 — токен бота
        tg_token = str(data[1][1]).strip() if len(data) > 1 and len(data[1]) > 1 else ''

        # A4+ — chat_id для отправки (строка 3 = индекс 2, далее)
        tg_chats = []
        for row in data[3:]:
            val = str(row[0]).strip() if row else ''
            if val:
                tg_chats.append(val)

        keywords = []
        negatives = []
        for row in data[1:]:
            # D (индекс 3) — ключевые слова
            if len(row) > 3 and row[3].strip():
                keywords.append(row[3].strip())
            # F (индекс 5) — негативные слова
            if len(row) > 5 and row[5].strip():
                negatives.append(row[5].strip())

        return keywords_enabled, keywords, negatives, tg_token, tg_chats

    except Exception as e:
        log.error('Ошибка чтения настроек: ' + str(e))
        return False, [], [], '', []


def get_channels(ss):
    """
    Лист «Каналы»:
      A = адрес канала
      B = last_link   (последний пост — обновляется скриптом)
      C = статус      (обновляется скриптом)
      D = peer_id     (числовой ID — кеш, заполняется автоматически)
      E = access_hash (заполняется автоматически)
    """
    try:
        sheet = ss.worksheet('Каналы')
        data = sheet.get_all_values()
        channels = []
        for i, row in enumerate(data[1:], start=2):
            if not row or not row[0].strip():
                continue
            addr = parse_channel_address(row[0].strip())
            if not addr:
                continue
            last_link = row[1].strip() if len(row) > 1 else ''
            peer_id_str = row[3].strip() if len(row) > 3 else ''
            peer_id = int(peer_id_str) if peer_id_str.lstrip('-').isdigit() else None
            access_hash_str = row[4].strip() if len(row) > 4 else ''
            access_hash = int(access_hash_str) if access_hash_str.lstrip('-').isdigit() else None
            channels.append({
                'raw': row[0].strip(),
                'addr': addr,
                'last_link': last_link,
                'peer_id': peer_id,
                'access_hash': access_hash,
                'row': i,
            })
        return channels
    except Exception as e:
        log.error('Ошибка чтения каналов: ' + str(e))
        return []


def update_channel(ss, row, last_link, status):
    try:
        sheet = ss.worksheet('Каналы')
        sheet.update([[last_link, status]], f'B{row}:C{row}')
    except Exception as e:
        log.error(f'Ошибка обновления канала row={row}: {e}')


def save_channel_ids(ss, row, peer_id: int, access_hash: int):
    try:
        sheet = ss.worksheet('Каналы')
        sheet.update([[str(peer_id), str(access_hash)]], f'D{row}:E{row}')
    except Exception as e:
        log.error(f'Ошибка сохранения peer_id/access_hash row={row}: {e}')


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
            p['text'],
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
        sheet.append_row(
            [datetime.now().strftime('%Y-%m-%d %H:%M:%S'), level, safe],
            value_input_option='USER_ENTERED',
        )
    except Exception as e:
        log.error('Ошибка записи лога: ' + str(e))


# ══════════════════════════════════════════════════════
#  Отправка в Telegram бот
# ══════════════════════════════════════════════════════

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
                url = f'https://api.telegram.org/bot{tg_token}/sendMessage'
                data = json.dumps({
                    'chat_id': chat_id,
                    'text': body,
                    'disable_web_page_preview': False,
                }).encode('utf-8')
                req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
                urllib.request.urlopen(req, timeout=10)
                time.sleep(0.3)
            except Exception as e:
                log.error(f'Ошибка отправки TG в {chat_id}: {e} | текст: {body[:200]}')
        time.sleep(0.3)


# ══════════════════════════════════════════════════════
#  Вспомогательные функции
# ══════════════════════════════════════════════════════

def extract_post_id(link: str) -> int:
    m = re.search(r'/(\d+)$', link)
    return int(m.group(1)) if m else 0


def build_link(chat, msg_id: int) -> str:
    username = getattr(chat, 'username', None)
    if username:
        return f'https://t.me/{username}/{msg_id}'
    chat_id = str(chat.id)
    if chat_id.startswith('-100'):
        chat_id = chat_id[4:]
    return f'https://t.me/c/{chat_id}/{msg_id}'


def get_author_info(msg):
    try:
        if not msg.sender:
            return '', ''
        sender = msg.sender
        first = getattr(sender, 'first_name', '') or ''
        last = getattr(sender, 'last_name', '') or ''
        username = getattr(sender, 'username', '') or ''
        full_name = (first + ' ' + last).strip()
        author_link = f'https://t.me/{username}' if username else ''
        return full_name, author_link
    except Exception:
        return '', ''


def matches_keywords(text: str, keywords: list) -> bool:
    if not text or not keywords:
        return False
    text_lower = text.lower()
    for kw in keywords:
        kw_lower = kw.lower().strip().rstrip('*')
        if kw_lower and kw_lower in text_lower:
            return True
    return False


def matches_negatives(text: str, negatives: list) -> bool:
    if not text or not negatives:
        return False
    text_lower = text.lower()
    for neg in negatives:
        neg_lower = neg.lower().strip().rstrip('*')
        if neg_lower and neg_lower in text_lower:
            return True
    return False


# ══════════════════════════════════════════════════════
#  Получение entity — защита от флуда
# ══════════════════════════════════════════════════════

async def resolve_with_flood_protection(client, identifier, label: str, max_retries: int = 3):
    cache_key = str(identifier)
    if cache_key in _entity_cache:
        return _entity_cache[cache_key]

    for attempt in range(1, max_retries + 1):
        try:
            entity = await client.get_entity(identifier)
            _entity_cache[cache_key] = entity
            return entity
        except FloodWaitError as e:
            wait_sec = e.seconds
            if wait_sec > MAX_FLOOD_WAIT_SEC:
                raise RuntimeError(f'FloodWait слишком большой ({wait_sec}s), канал пропущен') from e
            log.warning(f'{label} | FloodWait {wait_sec}s (попытка {attempt}/{max_retries}), жду...')
            await asyncio.sleep(wait_sec + 2)
        except (UsernameInvalidError, UsernameNotOccupiedError) as e:
            raise RuntimeError(f'Неверный username: {e}') from e
        except (ChannelPrivateError, ChatAdminRequiredError) as e:
            raise RuntimeError(f'Канал недоступен: {e}') from e

    raise RuntimeError(f'{label} | Не удалось получить entity после {max_retries} попыток')


async def get_chat_entity(client, ch: dict) -> tuple:
    """
    Возвращает (chat_entity, is_new).
    Приоритет (от быстрого к медленному):
      1. peer_id + access_hash → InputPeerChannel  (0 API запросов)
      2. Приватный channel_id из адреса → session cache
      3. Публичный peer_id сохранён → session cache
      4. Публичный username → ResolveUsernameRequest (только первый раз)
    """
    addr = ch['addr']
    peer_id = ch['peer_id']
    access_hash = ch['access_hash']
    label = ch['raw']

    if peer_id is not None and access_hash is not None:
        cache_key = f'input_{peer_id}'
        if cache_key not in _entity_cache:
            peer = InputPeerChannel(channel_id=peer_id, access_hash=access_hash)
            entity = await client.get_entity(peer)
            _entity_cache[cache_key] = entity
        return _entity_cache[cache_key], False

    if addr['type'] == 'private' and addr['channel_id'] is not None:
        try:
            entity = await resolve_with_flood_protection(client, addr['channel_id'], label)
            return entity, True
        except Exception:
            raise RuntimeError(
                f'Приватный канал {addr["channel_id"]}: нет в session cache. '
                f'Убедитесь что аккаунт состоит в этом канале/группе.'
            )

    if peer_id is not None:
        entity = await resolve_with_flood_protection(client, peer_id, label)
        return entity, False

    if addr['username']:
        entity = await resolve_with_flood_protection(client, addr['username'], label)
        return entity, True

    raise RuntimeError(f'Не удалось определить идентификатор для канала: {label}')


# ══════════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════════

async def main():
    log.info(f'Запуск прогона | SPREADSHEET_ID: {SPREADSHEET_ID[:8]}...')

    try:
        ss = get_spreadsheet()
        log.info('Google Sheets подключён')
    except Exception as e:
        log.error('Ошибка Google Sheets: ' + str(e))
        return

    keywords_enabled, keywords, negatives, tg_token, tg_chats = get_settings(ss)
    channels = get_channels(ss)

    log.info(
        f'Чатов: {len(channels)} | '
        f'Ключи: {"ВКЛ (" + str(len(keywords)) + " шт)" if keywords_enabled else "ВЫКЛ"} | '
        f'Негативы: {len(negatives)} шт | '
        f'TG чатов для отправки: {len(tg_chats)}'
    )

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
    total_skipped_neg = 0

    write_log(ss, 'INFO',
        f'ПРОГОН НАЧАТ | чатов: {len(channels)} | '
        f'ключи: {"ВКЛ (" + str(len(keywords)) + " шт)" if keywords_enabled else "ВЫКЛ"} | '
        f'негативы: {len(negatives)} шт'
    )

    for ch in channels:
        label = ch['raw']
        last_link = ch['last_link']
        last_post_id = extract_post_id(last_link) if last_link else 0
        row = ch['row']

        try:
            chat, is_new = await get_chat_entity(client, ch)

            if is_new:
                ah = getattr(chat, 'access_hash', 0) or 0
                save_channel_ids(ss, row, chat.id, ah)
                log.info(f'{label} | сохранён peer_id={chat.id} access_hash={ah}')

            chat_name = getattr(chat, 'title', None) or getattr(chat, 'username', None) or label

            since = datetime.now(timezone.utc) - timedelta(minutes=LOOKBACK_MINUTES)
            messages = []

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
            skipped_neg = 0
            new_last_link = last_link

            for msg in messages:
                if msg.action is not None:
                    continue

                text = msg.text or msg.message or ''
                if hasattr(msg, 'caption') and msg.caption:
                    text = msg.caption
                text = ' '.join(text.split())

                author_name, author_link = get_author_info(msg)
                link = build_link(chat, msg.id)
                date = msg.date.replace(tzinfo=None)
                new_last_link = link

                # Фильтр ключевых слов
                if keywords_enabled and keywords:
                    if text.strip():
                        if not matches_keywords(text, keywords):
                            continue
                    else:
                        continue

                # Фильтр негативных слов (работает всегда если заданы)
                if negatives and text.strip():
                    if matches_negatives(text, negatives):
                        skipped_neg += 1
                        log.info(f'{label} | пост {msg.id} отклонён по негативу')
                        continue

                saved_msgs.append({
                    'date': date,
                    'chat_name': chat_name,
                    'author_name': author_name,
                    'author_link': author_link,
                    'link': link,
                    'text': text,
                })

            total_new += new_msgs_count
            total_saved += len(saved_msgs)
            total_skipped_neg += skipped_neg
            all_new_posts.extend(saved_msgs)

            if new_msgs_count > 0:
                status = f'✅ Новых: {new_msgs_count} | Записано: {len(saved_msgs)}'
                if skipped_neg:
                    status += f' | Негативов: {skipped_neg}'
                update_channel(ss, row, new_last_link, status)
            else:
                update_channel(ss, row, last_link, '✅ Нет новых сообщений')

            log.info(
                f'{label} | новых: {new_msgs_count} | записано: {len(saved_msgs)} | '
                f'негативов: {skipped_neg} | lastId: {last_post_id or "пусто"}'
            )

        except FloodWaitError as e:
            wait_sec = e.seconds
            log.warning(f'{label} | FloodWait {wait_sec}s, пропускаю канал')
            update_channel(ss, row, last_link, f'⏳ FloodWait {wait_sec}s')
            write_log(ss, 'WARN', f'{label} | FloodWait {wait_sec}s')
        except Exception as e:
            log.error(f'{label} | ОШИБКА: {e}')
            update_channel(ss, row, last_link, '❌ Ошибка: ' + str(e)[:50])
            write_log(ss, 'ERROR', f'{label} | {str(e)[:100]}')

        await asyncio.sleep(2)

    write_posts(ss, all_new_posts)

    if all_new_posts and tg_token and tg_chats:
        log.info(f'Отправляю {len(all_new_posts)} постов в TG...')
        send_to_telegram(all_new_posts, tg_token, tg_chats)

    summary = (
        f'ПРОГОН ЗАВЕРШЁН | чатов: {len(channels)} | '
        f'новых: {total_new} | записано: {total_saved} | '
        f'негативов: {total_skipped_neg}'
    )
    log.info(summary)
    write_log(ss, 'INFO', summary)

    await client.disconnect()


if __name__ == '__main__':
    asyncio.run(main())
