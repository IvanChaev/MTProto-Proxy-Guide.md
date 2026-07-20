# Подключение Telegram-бота через MTProto-прокси (TgWsProxy + Telethon)

Гайд объясняет, как подключить Telegram-бота на [Telethon](https://github.com/LonamiWebs/Telethon)
к [TgWsProxy](https://github.com/Flowseal/tg-ws-proxy) — локальному MTProto-прокси. В отличие от
базовых гайдов на эту тему, здесь конфиг прокси ищется автоматически и кроссплатформенно,
а не по одному захардкоженному пути.

## Идея

TgWsProxy поднимает локальный MTProto-прокси и пишет свой конфиг (`host`, `port`, `secret`) в
стандартную для ОС папку с настройками приложений. Задача бота — найти этот конфиг и передать
данные в Telethon через `ConnectionTcpMTProxyAbridged`.

Проблема большинства гайдов (и моей предыдущей версии в том числе) — путь к конфигу зашит
буквально: `C:\Users\User\AppData\Roaming\TgWsProxy\config.json`. Это работает только на конкретной
машине конкретного человека. У другого юзера другое имя учётки, а на Linux/macOS такого пути вообще
нет. Ниже — способ находить конфиг правильно на любой системе.

## Где на самом деле лежит конфиг

| ОС | Путь |
|---|---|
| Windows | `%APPDATA%\TgWsProxy\config.json` |
| macOS | `~/Library/Application Support/TgWsProxy/config.json` |
| Linux | `$XDG_CONFIG_HOME/tgwsproxy/config.json` (обычно `~/.config/tgwsproxy/config.json`) |

`%APPDATA%` и `$XDG_CONFIG_HOME` — это переменные окружения самой ОС, они не завязаны на конкретное
имя пользователя, поэтому один и тот же код работает у всех.

Пример содержимого файла:

```json
{
  "host": "127.0.0.1",
  "port": 1080,
  "secret": "a05020da9c1a504d83fd66ec5c916eb1"
}
```

## Чтение секрета

```python
import json
import os
import platform
from pathlib import Path


def get_proxy_config_path() -> Path:
    system = platform.system()
    if system == "Windows":
        base = os.getenv("APPDATA", str(Path.home() / "AppData" / "Roaming"))
        return Path(base) / "TgWsProxy" / "config.json"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "TgWsProxy" / "config.json"
    base = os.getenv("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(base) / "tgwsproxy" / "config.json"


def read_proxy_config() -> dict | None:
    path = get_proxy_config_path()
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    if "secret" not in data:
        return None
    return {
        "host": data.get("host", "127.0.0.1"),
        "port": int(data.get("port", 1080)),
        "secret": data["secret"],
    }
```

Секрет отдаём в Telethon как есть, без ручных манипуляций с префиксами вроде `dd` — библиотека сама
корректно разбирает разные форматы MTProto-секретов, и подмена префикса вручную только ломает
подключение у части конфигураций.

## Подключение клиента

`ConnectionTcpMTProxyAbridged` из `telethon.network` — класс соединения, который умеет говорить с
MTProto-прокси. Прокси передаётся кортежем `(host, port, secret)`:

```python
from telethon import TelegramClient
from telethon.network import ConnectionTcpMTProxyAbridged

cfg = read_proxy_config()
proxy = (cfg["host"], cfg["port"], cfg["secret"]) if cfg else None

client = TelegramClient(
    "bot_session",
    api_id=API_ID,
    api_hash=API_HASH,
    connection=ConnectionTcpMTProxyAbridged if proxy else None,
    proxy=proxy,
)

await client.start(bot_token=BOT_TOKEN)
```

Если конфиг прокси не найден, `proxy` остаётся `None` и Telethon просто подключается напрямую —
гайд не завязывает бота на обязательное наличие прокси.

## Переподключение при обрыве

Сеть до прокси не всегда стабильна, особенно сразу после его запуска. Есть смысл оборачивать
`client.start()` в ретрай:

```python
from telethon.errors import FloodWaitError

async def connect_with_retry(client, bot_token, max_retries=5, delay=5):
    for attempt in range(max_retries):
        try:
            await client.start(bot_token=bot_token)
            return True
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds)
        except (ConnectionError, OSError, asyncio.TimeoutError):
            await asyncio.sleep(delay)
    return False
```

## Почему это работает

- `ConnectionTcpMTProxyAbridged` — встроенный в Telethon класс, реализующий протокол MTProto Proxy
  (обфускация случайными байтами + секретный префикс).
- Сервер Telegram видит такое соединение как легитимное, даже если прямой доступ к Telegram API
  заблокирован на уровне сети.
- Дополнительные зависимости вроде `python-socks` не нужны — MTProto-прокси поддерживается Telethon
  из коробки.

## Ссылки

- Telethon: https://github.com/LonamiWebs/Telethon
- Документация MTProto Proxy: https://core.telegram.org/mtproto/mtproto-transports#mtproxy
- TgWsProxy: https://github.com/Flowseal/tg-ws-proxy
