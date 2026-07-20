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
# Этот файл необходим исключительно для того чтобы github автоматически дал репозиторию иконку python
