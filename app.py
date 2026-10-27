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
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "deepseek-web")  # deepseek-web | openrouter | none
LLM_BASE = os.environ.get("LLM_BASE", "https://openrouter.ai/api/v1")
LLM_KEY = os.environ.get("LLM_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek/deepseek-chat-v3-0324:free")
# токен сессии chat.deepseek.com (cookie userToken), для LLM_PROVIDER=deepseek-web
DEEPSEEK_TOKEN = os.environ.get("DEEPSEEK_TOKEN", "")
DEEPSEEK_BASE = os.environ.get("DEEPSEEK_BASE", "https://chat.deepseek.com/api/v0")
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
    prompt = f"{SYSTEM_PROMPT}\n\nФраза пользователя: {text}"
    content = ""
    try:
        if LLM_PROVIDER == "deepseek-web" and DEEPSEEK_TOKEN:
            content = deepseek_web_ask(prompt)
        elif LLM_PROVIDER == "openrouter" and LLM_KEY:
            content = openrouter_ask(prompt)
    except Exception as exc:  # сеть/смена вёрстки — уходим в офлайн-разбор
        print(f"[!] LLM error: {exc}")
        content = ""
    if not content:
        return offline_intent(text)
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        return {"action": "chat", "reply": content.strip()[:200] or "не поняла запрос"}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return {"action": "chat", "reply": content.strip()[:200]}


def offline_intent(text: str) -> dict[str, Any]:
    """Разбор без LLM: простая эвристика, чтобы колонка не молчала."""
    m = re.match(r"(?:включи|поставь|сыграй)\s+(.+)", text, re.IGNORECASE)
    if m:
        return {"action": "play", "query": m.group(1).strip(" .,!?"), "reply": "включаю"}
    m = re.match(r"(?:выключи|стоп|останови)", text, re.IGNORECASE)
    if m:
        return {"action": "stop", "reply": "останавливаю"}
    m = re.search(r"(?:громкость|звук)\D{0,10}(\d{1,3})", text, re.IGNORECASE)
    if m:
        return {"action": "volume", "volume": min(100, int(m.group(1))), "reply": "меняю громкость"}
    return {"action": "chat", "reply": "мозг не настроен"}


def openrouter_ask(prompt: str) -> str:
    response = _client.post(
        f"{LLM_BASE}/chat/completions",
        json={
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 250,
            "temperature": 0.2,
        },
        headers={"authorization": f"Bearer {LLM_KEY}"},
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


# --------------------------------------------------------------------------- #
# Веб-DeepSeek: чат chat.deepseek.com по токену сессии (свой аккаунт)
# Неофициально: при редизайне сайта может сломаться, следи за логами.
# --------------------------------------------------------------------------- #
_ds_session: dict[str, Any] = {"id": None, "ts": 0.0}


def _ds_headers() -> dict[str, str]:
    return {
        "authorization": f"Bearer {DEEPSEEK_TOKEN}",
        "content-type": "application/json",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36",
        "accept": "*/*",
        "origin": "https://chat.deepseek.com",
        "referer": "https://chat.deepseek.com/",
    }


def _ds_new_session() -> str:
    response = _client.post(
        f"{DEEPSEEK_BASE}/chat_session/create",
        json={"agent": "chat"},
        headers=_ds_headers(),
    )
    response.raise_for_status()
    data = response.json().get("data") or {}
    session = data.get("chat_session") or data
    session_id = session.get("id")
    if not session_id:
        raise RuntimeError(f"не удалось создать сессию DeepSeek: {str(data)[:200]}")
    _ds_session["id"] = session_id
    _ds_session["ts"] = time.time()
    return session_id


def deepseek_web_ask(prompt: str) -> str:
    # сессию переиспользуем, обновляем раз в 30 минут
    session_id = _ds_session["id"]
    if not session_id or time.time() - _ds_session["ts"] > 1800:
        session_id = _ds_new_session()
    with _client.stream(
        "POST",
        f"{DEEPSEEK_BASE}/chat/completion",
        json={"chat_session_id": session_id, "parent_message_id": None, "prompt": prompt,
              "ref_file_ids": [], "thinking_enabled": False},
        headers=_ds_headers(),
        timeout=90,
    ) as response:
        response.raise_for_status()
        chunks: list[str] = []
        for line in response.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload in ("", "[DONE]"):
                continue
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue
            # у веб-чата разные формы события — собираем любой текст из content
            for fragment in _iter_content(event):
                chunks.append(fragment)
        return "".join(chunks)


def _iter_content(node: Any):
    if isinstance(node, dict):
        value = node.get("content")
        if isinstance(value, str) and value:
            yield value
        for key, item in node.items():
            if key != "content":
                yield from _iter_content(item)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_content(item)


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
