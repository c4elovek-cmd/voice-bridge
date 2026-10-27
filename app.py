#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Голосовой мост — серверная часть для VPS.

Принимает транскрипты от YAM-плагина на колонке, спрашивает LLM,
возвращает намерение (play/stop/volume/...) и поисковый запрос.

Защита: все ручки кроме /health требуют заголовок X-Voice-Token.
Запуск на сервере под Caddy (TLS) или напрямую на 127.0.0.1:8811.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="voice-bridge", docs_url=None, redoc_url=None)

TOKEN = os.environ.get("VOICE_TOKEN", "")
LLM_BASE = os.environ.get("LLM_BASE", "https://openrouter.ai/api/v1")
LLM_KEY = os.environ.get("LLM_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek/deepseek-chat-v3-0324:free")
# прокси для ссылок, которые могут из РФ не открываться (yt-dlp и т.д.)
PROXY_URL = os.environ.get("VOICE_PROXY", "")
STATE_PATH = Path(os.environ.get("VOICE_STATE", "voice-bridge-state.json"))

# --------------------------------------------------------------------------- #
# Старт
# --------------------------------------------------------------------------- #
if not TOKEN:
    TOKEN = secrets.token_urlsafe(24)
    STATE_PATH.write_text(json.dumps({"token": TOKEN, "created": time.time()}), encoding="utf-8")
    print(f"[i] VOICE_TOKEN не задан, сгенерирован и сохранён в {STATE_PATH}: {TOKEN}")

_client = httpx.Client(
    timeout=30,
    proxy=PROXY_URL or None,
    headers={"user-agent": "voice-bridge/0.1"},
)


# --------------------------------------------------------------------------- #
# Мозг: текст → намерение
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = (
    "Ты — голосовой ассистент в умной колонке. Пользователь сказал фразу. "
    "Выдели намерение и верни СТРОГО один JSON-объект без пояснений:\n"
    '{"action": "play|stop|next|volume|clock|status|chat", '
    '"query": "что искать (для play)", "volume": 0-100 (для volume), '
    '"reply": "короткий голосовой ответ для пользователя"}\n'
    "Для музыки: если просят включить что-то конкретное — action=play, в query — "
    "исполнитель и название. Если просят просто музыку — query='подборка'. "
    "На болтовню — action=chat, в reply — ответ в 1-2 предложениях."
)


async def ask_llm(text: str) -> dict[str, Any]:
    if not LLM_KEY:
        # без ключа — минимальный разбор: считается «музыка», если похоже на просьбу
        m = re.match(r"включ(?:и|ите)\s+(.+)", text, re.IGNORECASE)
        if m:
            return {"action": "play", "query": m.group(1), "reply": "включаю"}
        return {"action": "chat", "reply": "LLM не настроена, ключ отсутствует"}
    response = _client.post(
        f"{LLM_BASE}/chat/completions",
        json={
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            "max_tokens": 200,
            "temperature": 0.2,
        },
    )
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    # модель может обернуть JSON в ```...``` — вытаскиваем
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        return {"action": "chat", "reply": "не поняла запрос"}
    return json.loads(match.group(0))


# --------------------------------------------------------------------------- #
# Поиск музыки: yt-dlp без установки через pip (бинарник рядом или в PATH)
# --------------------------------------------------------------------------- #
def resolve_track(query: str) -> dict[str, Any] | None:
    """Найти трек на YouTube Music, вернуть {title, url} — прямую ссылку на поток."""
    import shutil
    import subprocess

    ytdlp = shutil.which("yt-dlp")
    if not ytdlp:
        return None
    cmd = [
        ytdlp,
        "--dump-json", "--no-playlist", "--no-warnings",
        "--default-search", "ytsearch1:",
        query,
    ]
    if PROXY_URL:
        cmd += ["--proxy", PROXY_URL]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            return None
        lines = [l for l in proc.stdout.splitlines() if l.strip()]
        if not lines:
            return None
        info = json.loads(lines[0])
        return {
            "title": info.get("title") or query,
            "url": info.get("url") or info.get("webpage_url"),
            "id": info.get("id"),
        }
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Ручки
# --------------------------------------------------------------------------- #
def auth(request: Request) -> None:
    if request.headers.get("x-voice-token", "") != TOKEN:
        raise HTTPException(status_code=401, detail="bad token")


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"ok": True})


@app.post("/api/voice")
async def api_voice(request: Request) -> JSONResponse:
    auth(request)
    data = await request.json()
    text = str(data.get("transcript", "")).strip()
    if not text:
        raise HTTPException(status_code=400, detail="transcript is empty")
    intent = await ask_llm(text)

    result: dict[str, Any] = {"intent": intent}
    if intent.get("action") == "play":
        track = resolve_track(intent.get("query") or text)
        result["track"] = track
    return JSONResponse(result)


@app.post("/api/tts")
async def api_tts(request: Request) -> JSONResponse:
    """Заготовка: ответ текстом, TTS пока нативный на колонке (aliced)."""
    auth(request)
    data = await request.json()
    return JSONResponse({"text": str(data.get("text", ""))[:500]})
