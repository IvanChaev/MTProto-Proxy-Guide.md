# Подключение Telegram-бота через MTProto-прокси (TgWsProxy + Telethon)

Гайд по подключению Telegram-бота на [Telethon](https://github.com/LonamiWebs/Telethon) к локальному
MTProto-прокси через [TgWsProxy](https://github.com/Flowseal/tg-ws-proxy). Описан рабочий путь,
проверенный на практике — только MTProto через TgWsProxy, без SOCKS5 и без прочих альтернатив,
которые на практике оказались нерабочими.

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
делается пинг TCP-соединением:

```python
import asyncio
import time

async def _ping_proxy(host, port, timeout=1.0):
    try:
        start = time.monotonic()
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        writer.close()
        await writer.wait_closed()
        return (time.monotonic() - start) * 1000
    except Exception:
        return None
```

Если пинг прошёл и меньше 500 мс — прокси считается рабочим:

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

    ping = await _ping_proxy(host, port)
    if ping is not None and ping < 500:
        return (secret, host, port)

    return None
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

`client.start()` на практике падает по-разному, и часть ошибок Telegram отдаёт не как типизированные
исключения Telethon, а как обычный `Exception` с текстом ошибки — поэтому часть обработки идёт через
разбор строки, а не через `except FloodWaitError`:

```python
import re
import asyncio
import logging

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
            logging.error(f"Таймаут подключения (попытка {attempt})")
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY)
            else:
                return False

        except Exception as e:
            error_str = str(e).lower()

            # Telegram иногда требует подождать N секунд перед авторизацией —
            # приходит НЕ как FloodWaitError, а как обычный текст исключения
            if "a wait of " in error_str and "seconds is required " in error_str:
                match = re.search(r'a wait of (\d+) seconds', error_str)
                if match:
                    wait_secs = int(match.group(1)) + 5
                    logging.warning(f"Требуется подождать {wait_secs} сек.")
                    await asyncio.sleep(wait_secs)
                    continue

            # Битая/заблокированная сессия — удаляем файлы сессии и пробуем заново
            if "database is locked " in error_str or "malformed " in error_str:
                logging.critical("Сессия повреждена. Удаляю для полного перелогина...")
                try:
                    if os.path.exists(session_path):
                        os.remove(session_path)
                    if os.path.exists(session_path + "-journal"):
                        os.remove(session_path + "-journal")
                except Exception:
                    pass
                continue

            if attempt < MAX_RETRIES:
                logging.warning(f"Ошибка подключения: {e}. Попытка {attempt}/{MAX_RETRIES}")
                await asyncio.sleep(RETRY_DELAY)
            else:
                logging.error(f"Не удалось подключиться после {MAX_RETRIES} попыток: {e}")
                return False
```

Эти три ветки обработки ошибок — не общая рекомендация "на всякий случай", а конкретные проблемы,
с которыми реально сталкивается это подключение: таймаут, скрытый под обычным текстом FloodWait,
и порча файла сессии при обрыве соединения.

## Шаг 5. Экстренный перезапуск прокси при сетевом сбое

Если во время работы (не при старте, а уже в рантайме — например, в фоновых задачах вроде мониторинга)
происходит сетевая ошибка, есть механизм экстренного перезапуска локального прокси. Он Windows-only:

```python
import subprocess
import os
import time

def _is_proxy_running(exe_path):
    try:
        import psutil
        exe_name = os.path.basename(exe_path).lower()
        for proc in psutil.process_iter(['name']):
            if proc.info['name'] and proc.info['name'].lower() == exe_name:
                return True
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

Проверка через `taskkill` — команда Windows, на Linux/macOS работать не будет. Если у тебя бот крутится
только на Windows, это не проблема; если планируешь переносить на другую ОС, эту функцию нужно
переписывать отдельно под неё.

## Почему именно так, а не иначе

- `ConnectionTcpMTProxyAbridged` — единственный подтверждённо рабочий вариант в этой связке.
  Списки альтернативных прокси (SOCKS5 и подобные) с этим классом соединения несовместимы на уровне
  протокола и в этом гайде не рассматриваются.
- Пинг перед использованием прокси нужен, потому что наличие корректного конфига не гарантирует,
  что прокси-процесс сейчас реально запущен и отвечает.
- Явная проверка "прокси не найден → не подключаемся" — намеренное решение, а не недоработка:
  подключение без прокси в этом сценарии не является рабочим вариантом.

## Ссылки

- Telethon: https://github.com/LonamiWebs/Telethon
- Документация MTProto Proxy: https://core.telegram.org/mtproto/mtproto-transports#mtproxy
- TgWsProxy: https://github.com/Flowseal/tg-ws-proxy

## Если не работает, то на это может быть масса причин, лучше в таком случае кидать файлы и гайд в нейросеть.


## Gemini 3.1 Pro:
Несмотря на практическую направленность, **Гайд имеет ряд архитектурных и технических уязвимостей**. Он может перестать работать из-за обновления библиотек, смены окружения или специфических сетевых условий.

Ниже подробно разобраны основные причины, по которым код из гайда может сломаться на практике.

### 1. Проблемы с версиями Telethon и зависимостями

* **Мажорные обновления Telethon (v1.x против v2.x):** Библиотека Telethon развивается, и в версии 2.0 (которая находится в разработке) структура модулей и сетевого стека существенно меняется. Класс `ConnectionTcpMTProxyAbridged`, импортируемый из `telethon.network`, или сам формат передачи кортежа `proxy=(host, port, secret)` могут быть объявлены устаревшими или изменить сигнатуру в новых версиях.

* **Типы MTProto-секретов:** Современные MTProto-прокси часто используют специальные префиксы для обхода DPI — например, `dd` (random padding) или `ee` (Fake-TLS с прикрепленным доменом). В зависимости от версии Telethon, передача «сырого» секрета с Fake-TLS домена в класс `ConnectionTcpMTProxyAbridged` может вызывать ошибки протокола, если парсер библиотеки старой версии не умеет отделять hex-ключ от домена.

* **Скрытая и «тихая» зависимость от `psutil`:** Функция проверки запущенного процесса `_is_proxy_running` пытается импортировать стороннюю библиотеку `psutil` прямо внутри тела функции. Если разработчик не установил её через `pip install psutil`, блок `try...except` молча перехватит `ModuleNotFoundError` и вернет `False`. В результате бот будет думать, что прокси выключен, и при каждом вызове функции `emergency_launch_proxy` будет пытаться «убить» и перезапустить процесс через `taskkill`.

### 2. Привязка к операционной системе (Windows-Only)

* **Несовместимость `taskkill` с Linux/macOS:** Функция экстренного перезапуска прокси использует системную команду Windows `taskkill /f /im`. При попытке запустить этот код в Docker-контейнере, на VPS под управлением Linux или на macOS, вызов `os.system` будет завершаться с ошибкой (или просто не выполнится), так как такой команды в POSIX-системах нет.

* **Блокировка файлов в Windows (`PermissionError`):** В коде предусмотрено удаление сессии при повреждении: `os.remove(session_path)`. Однако в Windows, если фоновый поток Telethon или другой процесс (например, антивирус) всё ещё удерживает дескриптор файла `.session` или `.session-journal`, операционная система не позволит его удалить и выбросит `PermissionError`. Так как в коде стоит глухой `except Exception: pass`, ошибка удалится молча, и бот уйдет в бесконечный цикл попыток подключиться к битой сессии.

### 3. Хрупкость сетевой логики (Пинг и сокеты)

* **Ложное срабатывание лимита в 500 мс:** В функции `init_proxy()` зашит жесткий лимит: прокси считается рабочим только если пинг `< 500` мс. Если бот запускается на слабом железе (например, Raspberry Pi), виртуальной машине под сильной нагрузкой CPU или в момент старта системы, время открытия локального сокета в функции `_ping_proxy` может кратковременно превысить 500 мс. В итоге `init_proxy()` вернет `None`, и бот завершит работу с ошибкой *«Не удалось найти рабочий прокси»*, хотя сам прокси исправен.

* **«Пинг проходит, но прокси мёртв»:** Функция `_ping_proxy` проверяет лишь способность TCP-сокета открыться и закрыться. Однако локальный порт может быть открыт, но сам процесс TgWsProxy при этом может зависнуть на уровне обработки MTProto-трафика или потерять связь с серверами Telegram. Также антивирусы/файрволы часто разрешают локальные TCP-соединения (пинг будет успешным), но блокируют нестандартный шифрованный трафик внутри сокета.

### 4. Ненадежный парсинг ошибок (Хардкод строк)

* **Зависимость от английского текста исключений:** В шаге 4 код пытается отловить ограничение по частоте запросов (FloodWait) не через стандартный класс исключения, а через поиск подстрок `"a wait of "` и `"seconds is required "` в тексте ошибки с помощью регулярного выражения.

* Если в новой версии Telethon или в самом API Telegram изменится формулировка текста ошибки (например, слово *"required"* заменят на *"needed"* или поменяется регистр), регулярное выражение `re.search(r'a wait of (\d+) seconds', error_str)` перестанет срабатывать.

* Бот не распознает требование подождать, упадет в ветку стандартной ошибки, исчерпает 10 попыток подключения (`MAX_RETRIES = 10`) и полностью прекратит работу.

### 5. Проблемы с внешним конфигурационным файлом

* **Жёсткая зависимость от структуры `config.py`:** Гайд подразумевает наличие внешнего модуля `config.py`, откуда импортируются переменные `PROXY_CONFIG_PATH`, `BOT_TOKEN`, `API_ID`, `API_HASH` и `TEMP_DIR`. Если разработчик переносит код в другой проект, где конфигурация хранится, например, в переменных окружения `.env` или в формате YAML, код придется переписывать.

* **Изменение формата конфига TgWsProxy:** Код ожидает, что в JSON-файле прокси обязательно будут ключи `"secret"`, `"host"` и `"port"`. Если автор утилиты TgWsProxy в будущем обновлении изменит структуру JSON (например, вложит настройки портов в отдельный объект `"listen": {"port": 1080}`), функция `init_proxy` перестанет находить данные и вернет `None`.

## Как исправить критические уязвимости (Рефакторинг)

### 1. Переход на типизированные исключения Telethon

Вместо парсинга строк через регулярные выражения стоит сначала ловить официальные исключения библиотеки, и только как fallback использовать regex.

```python
import asyncio
import logging
import os
import re
from telethon import TelegramClient
from telethon.errors import FloodWaitError, AuthKeyError

MAX_RETRIES = 10
RETRY_DELAY = 10

async def connect_with_retry(client: TelegramClient, bot_token: str, session_path: str) -> bool:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            await asyncio.wait_for(client.start(bot_token=bot_token), timeout=30)
            logging.info("Бот успешно стартовал")
            return True

        except asyncio.TimeoutError:
            logging.error(f"Таймаут подключения (попытка {attempt}/{MAX_RETRIES})")

        # 1. Ловим стандартный FloodWait от Telethon
        except FloodWaitError as e:
            wait_secs = e.seconds + 5
            logging.warning(f"Telegram требует подождать {wait_secs} сек. (FloodWaitError)")
            await asyncio.sleep(wait_secs)
            continue

        except Exception as e:
            error_str = str(e).lower()

            # 2. Fallback для нестандартных текстовых ошибок ожидания
            if "wait of" in error_str and "seconds" in error_str:
                match = re.search(r'wait of (\d+) seconds', error_str)
                if match:
                    wait_secs = int(match.group(1)) + 5
                    logging.warning(f"Требуется подождать {wait_secs} сек. (Text match)")
                    await asyncio.sleep(wait_secs)
                    continue

            # 3. Обработка битых сессий с проверкой прав доступа
            if any(err in error_str for err in ("database is locked", "malformed", "authkey")):
                logging.critical("Сессия повреждена. Пытаюсь удалить...")
                for suffix in ("", "-journal"):
                    file_to_remove = f"{session_path}{suffix}"
                    if os.path.exists(file_to_remove):
                        try:
                            os.remove(file_to_remove)
                        except PermissionError:
                            logging.error(f"Файл {file_to_remove} заблокирован ОС. Нужен ручной перезапуск.")
                            return False
                continue

            logging.warning(f"Непредвиденная ошибка: {e}. Попытка {attempt}/{MAX_RETRIES}")

        if attempt < MAX_RETRIES:
            await asyncio.sleep(RETRY_DELAY)
            
    return False

```

### 2. Кроссплатформенный перезапуск прокси без тихих сбоев

Чтобы код не падал молча без `psutil` и работал как на Windows, так и на Linux, добавляем явную проверку ОС и логирование ошибки импорта.

```python
import subprocess
import os
import sys
import time
import logging

def _is_proxy_running(exe_name: str) -> bool:
    try:
        import psutil
        for proc in psutil.process_iter(['name']):
            if proc.info['name'] and proc.info['name'].lower() == exe_name.lower():
                return True
        return False
    except ImportError:
        logging.error("Библиотека psutil не установлена! Проверка процессов невозможна.")
        # Возвращаем True, чтобы не пытаться бесконечно убивать процесс вслепую
        return True

def emergency_launch_proxy(exe_path: str):
    exe_name = os.path.basename(exe_path)
    if _is_proxy_running(exe_name):
        return

    logging.warning(f"Прокси {exe_name} не найден. Пытаюсь перезапустить...")
    
    if sys.platform == "win32":
        os.system(f'taskkill /f /im {exe_name} >nul 2>&1')
        time.sleep(1)
        subprocess.Popen(exe_path, shell=False)
    elif sys.platform.startswith("linux"):
        # Аналог для Linux (например, если прокси запущен как systemd-сервис или бинарник)
        os.system(f'pkill -f {exe_name} > /dev/null 2>&1')
        time.sleep(1)
        subprocess.Popen(["nohup", exe_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        logging.error(f"Автоперезапуск не реализован для ОС: {sys.platform}")

```

### 3. Убираем хрупкий пинг в 500 мс

Для локального сокета (`127.0.0.1`) замер задержки вообще не имеет смысла — TCP-хэндшейк внутри loopback-интерфейса всегда быстрый, если процесс жив. Ограничение в `500 мс` только создаёт ложные срабатывания при скачах нагрузки на CPU.

Достаточно проверять сам факт успешного открытия сокета с таймаутом в `1.5` секунды:

```python
async def _is_socket_open(host: str, port: int, timeout: float = 1.5) -> bool:
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
