# Подключение Telegram-бота через MTProto-прокси (TgWsProxy + Telethon)

Гайд по подключению Telegram-бота на [Telethon](https://github.com/LonamiWebs/Telethon) к локальному
MTProto-прокси через [TgWsProxy](https://github.com/Flowseal/tg-ws-proxy). Описан рабочий путь,
проверенный на практике — только MTProto через TgWsProxy, без SOCKS5 и без прочих альтернатив,
которые на практике оказались нерабочими.

«Программное обеспечение предоставляется "как есть" (AS IS), без каких-либо гарантий».

## Требования

- Python 3.8+
- `pip install "telethon<2.0" psutil`

`psutil` нужен только для шага 5 — проверки, что процесс прокси реально жив.

Версия Telethon зафиксирована намеренно: начиная с v2.0 разработчик полностью переписал внутренний
сетевой стек, и старый импорт `ConnectionTcpMTProxyAbridged` из `telethon.network` в новых версиях
может измениться или исчезнуть. Ограничение `"telethon<2.0"` гарантированно ставит стабильную ветку
v1.x, где этот класс существует и работает ровно так, как описано в гайде.

## Идея

TgWsProxy поднимает локальный MTProto-прокси и пишет свой конфиг (`host`, `port`, `secret`) в
файл на диске. Бот читает этот конфиг, проверяет, что прокси реально отвечает, и подключается через
Telethon с явным классом `ConnectionTcpMTProxyAbridged`.

Прокси в этой схеме — не опциональная штука "для обхода блокировок, если получится". Он обязателен:
если рабочий прокси не найден, бот не запускается вообще, а не пытается подключиться напрямую.

## Шаг 1. Чтение конфига TgWsProxy

Путь до конфига хранится не в самом модуле, а во внешнем `config.py` (переменная `PROXY_CONFIG_PATH`) —
это позволяет не завязывать код на конкретный путь конкретного пользователя жёстко внутри логики прокси:

```python
import os
import json
from config import PROXY_CONFIG_PATH

def _read_full_proxy_config():
    if not os.path.exists(PROXY_CONFIG_PATH):
        return None
    try:
        with open(PROXY_CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None
```

`PROXY_CONFIG_PATH` в `config.py` указывает на файл, который пишет сам TgWsProxy (обычно это
что-то вроде `%APPDATA%\TgWsProxy\config.json` на Windows — зависит от того, как настроен именно
твой TgWsProxy).

## Шаг 2. Проверка, что прокси реально живой

Прочитать секрет из конфига — не гарантия, что прокси сейчас отвечает. Поэтому перед использованием
проверяется, что локальный TCP-сокет реально открывается:

```python
import asyncio

async def _is_socket_open(host, port, timeout=1.5):
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        writer.close()
        await writer.wait_closed()
        return True
    except (asyncio.TimeoutError, ConnectionRefusedError, OSError):
        return False
```

Проверяется только факт открытия соединения — без замера задержки. Для локального сокета
(127.0.0.1) время отклика не информативно: если процесс жив, TCP-хэндшейк всегда быстрый, а жёсткий
лимит вроде 500 мс на нагруженной машине даёт только ложные ошибки «прокси не найден».

Если сокет открывается — прокси считается рабочим:

```python
async def init_proxy():
    config = _read_full_proxy_config()
    if not config:
        return None

    secret = config.get("secret")
    host = config.get("host", "127.0.0.1")
    port = config.get("port", 1080)

    if not (secret and host and port):
        return None

    if not await _is_socket_open(host, port):
        return None

    return (secret, host, port)
```

Обрати внимание на **порядок значений в кортеже**: здесь это `(secret, host, port)`. Это важно
запомнить, потому что при передаче в Telethon порядок другой (см. шаг 3) — если их перепутать,
подключение сломается молча.

## Шаг 3. Инициализация клиента

```python
from telethon import TelegramClient
from telethon.network import ConnectionTcpMTProxyAbridged
from config import BOT_TOKEN, API_ID, API_HASH, TEMP_DIR

best = await init_proxy()
if not best:
    # рабочего прокси нет — бот НЕ подключается напрямую, а прерывает запуск
    raise RuntimeError("Не удалось найти рабочий прокси")

secret, proxy_host, proxy_port = best

# Порядок для Telethon другой: (host, port, secret)
proxy = (proxy_host, proxy_port, secret)

session_path = os.path.join(TEMP_DIR, "bot_monitor")  # своя папка проекта, не системный temp

client = TelegramClient(
    session_path,
    api_id=API_ID,
    api_hash=API_HASH,
    connection=ConnectionTcpMTProxyAbridged,
    proxy=proxy
)

await client.start(bot_token=BOT_TOKEN)
```

`TEMP_DIR` — переменная из `config.py` проекта, указывающая на рабочую папку для файла сессии.
Это не системная temp-папка ОС, а папка внутри самого проекта.

## Шаг 4. Подключение с ретраями и обработкой специфичных ошибок

`client.start()` на практике падает по-разному. Большинство ошибок Telegram приходит как
типизированные исключения Telethon (например, `FloodWaitError`), но часть — как обычный `Exception`
с текстом ошибки. Поэтому обработка идёт в два захода: сначала официальные классы исключений,
а разбор строки остаётся как запасной вариант:

```python
import re
import os
import asyncio
import logging

from telethon.errors import FloodWaitError

MAX_RETRIES = 10
RETRY_DELAY = 10

async def connect_with_retry(client):
    session_path = os.path.join(TEMP_DIR, "bot_monitor.session")

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            await asyncio.wait_for(client.start(bot_token=BOT_TOKEN), timeout=30)
            logging.info("Бот успешно стартовал")
            return True

        except asyncio.TimeoutError:
            logging.error(f"Таймаут подключения (попытка {attempt}/{MAX_RETRIES})")

        # Типизированный FloodWait от Telethon — надёжнее, чем парсинг текста ошибки
        except FloodWaitError as e:
            wait_secs = e.seconds + 5
            logging.warning(f"Telegram требует подождать {wait_secs} сек.")
            await asyncio.sleep(wait_secs)
            continue

        except Exception as e:
            error_str = str(e).lower()

            # Fallback: часть ошибок ожидания всё ещё приходит обычным текстом,
            # а не типизированным исключением
            if "wait of" in error_str and "seconds" in error_str:
                match = re.search(r'wait of (\d+) seconds', error_str)
                if match:
                    wait_secs = int(match.group(1)) + 5
                    logging.warning(f"Требуется подождать {wait_secs} сек.")
                    await asyncio.sleep(wait_secs)
                    continue

            # Битая/заблокированная сессия — удаляем файлы сессии и пробуем заново
            if any(err in error_str for err in ("database is locked", "malformed", "authkey")):
                logging.critical("Сессия повреждена. Удаляю для полного перелогина...")
                for suffix in ("", "-journal"):
                    file_to_remove = f"{session_path}{suffix}"
                    if os.path.exists(file_to_remove):
                        try:
                            os.remove(file_to_remove)
                        except PermissionError:
                            logging.error(f"Файл {file_to_remove} заблокирован ОС — нужен ручной перезапуск.")
                            return False
                continue

            logging.warning(f"Ошибка подключения: {e}. Попытка {attempt}/{MAX_RETRIES}")

        if attempt < MAX_RETRIES:
            await asyncio.sleep(RETRY_DELAY)
        else:
            logging.error(f"Не удалось подключиться после {MAX_RETRIES} попыток")
            return False

    return False
```

Эти ветки обработки ошибок — не общая рекомендация "на всякий случай", а конкретные проблемы,
с которыми реально сталкивается это подключение: таймаут, требование Telegram подождать
(типизированное или скрытое под обычным текстом исключения) и порча файла сессии при обрыве
соединения. Удаление битой сессии не глотает ошибку молча: если файл залочен ОС (типичная ситуация
для Windows), будет явный лог с просьбой перезапустить бота вручную, а не бесконечный цикл попыток
подключиться к битой сессии.

## Шаг 5. Экстренный перезапуск прокси при сетевом сбое

Если во время работы (не при старте, а уже в рантайме — например, в фоновых задачах вроде мониторинга)
происходит сетевая ошибка, есть механизм экстренного перезапуска локального прокси. Он Windows-only:

```python
import subprocess
import os
import time
import logging

def _is_proxy_running(exe_path):
    try:
        import psutil
        exe_name = os.path.basename(exe_path).lower()
        for proc in psutil.process_iter(['name']):
            if proc.info['name'] and proc.info['name'].lower() == exe_name:
                return True
    except ImportError:
        logging.error("psutil не установлен (pip install psutil) — проверка процессов невозможна")
        return True  # не пытаемся вслепую убивать и перезапускать процесс
    except Exception:
        pass
    return False

def emergency_launch_proxy(exe_path):
    if _is_proxy_running(exe_path):
        return  # прокси уже жив, не трогаем

    exe_name = os.path.basename(exe_path)
    os.system(f'taskkill /f /im {exe_name} >nul 2>&1')
    time.sleep(1)
    subprocess.Popen(exe_path, shell=False)
```

Проверка процесса использует библиотеку `psutil` — она есть в разделе «Требования». Без неё
функция не будет молча считать прокси мёртвым и бесконечно пытаться его перезапускать: вместо этого
она залогирует ошибку и решит, что прокси жив.

Проверка через `taskkill` — команда Windows, на Linux/macOS работать не будет. Если у тебя бот крутится
только на Windows, это не проблема; если планируешь переносить на другую ОС, эту функцию нужно
переписывать отдельно под неё.

## Почему именно так, а не иначе

- `ConnectionTcpMTProxyAbridged` — единственный подтверждённо рабочий вариант в этой связке.
  Списки альтернативных прокси (SOCKS5 и подобные) с этим классом соединения несовместимы на уровне
  протокола и в этом гайде не рассматриваются.
- Проверка сокета перед использованием прокси нужна, потому что наличие корректного конфига не
  гарантирует, что прокси-процесс сейчас реально запущен и отвечает. Для локального сокета замер
  задержки не нужен — достаточно факта, что соединение открылось, поэтому жёсткого лимита пинга нет.
- FloodWait ловится в два захода: сначала типизированное исключение `FloodWaitError`, затем, как
  запасной вариант, разбор текста ошибки — формулировки текстовых ошибок могут меняться между
  версиями Telethon.
- Явная проверка "прокси не найден → не подключаемся" — намеренное решение, а не недоработка:
  подключение без прокси в этом сценарии не является рабочим вариантом.

## Ссылки

- Telethon: https://github.com/LonamiWebs/Telethon
- Документация MTProto Proxy: https://core.telegram.org/mtproto/mtproto-transports#mtproxy
- TgWsProxy: https://github.com/Flowseal/tg-ws-proxy

Если не работает — причин может быть масса, лучше в таком случае кидать файлы и этот гайд в нейросеть.
