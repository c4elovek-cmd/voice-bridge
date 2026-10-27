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

import asyncio
import base64
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
# токен сессии веб-чата (userToken из localStorage chat.deepseek.com), для deepseek-web
DEEPSEEK_TOKEN = os.environ.get("DEEPSEEK_TOKEN", "")
# куки веб-чата опционально: aws-waf-token (анти-бот AWS WAF) может понадобиться.
# Формат: "name=value; name2=value2"
DEEPSEEK_COOKIE = os.environ.get("DEEPSEEK_COOKIE", "")
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
    headers = {
        "authorization": f"Bearer {DEEPSEEK_TOKEN}",
        "content-type": "application/json",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36",
        "accept": "*/*",
        "origin": "https://chat.deepseek.com",
        "referer": "https://chat.deepseek.com/",
    }
    if DEEPSEEK_COOKIE:
        headers["cookie"] = DEEPSEEK_COOKIE
    return headers


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
# Поиск музыки: источники на выбор (yt-dlp умеет YouTube и SoundCloud,
# Spotify даёт метаданные — воспроизводим найденное из открытых источников)
# --------------------------------------------------------------------------- #
BRIDGE_CONFIG_PATH = Path(os.environ.get("BRIDGE_CONFIG", "bridge-config.json"))
BRIDGE_CONFIG: dict[str, Any] = {"source": "youtube"}
if BRIDGE_CONFIG_PATH.exists():
    try:
        BRIDGE_CONFIG.update(json.loads(BRIDGE_CONFIG_PATH.read_text(encoding="utf-8")))
    except Exception as exc:
        print(f"[!] bridge config не читается: {exc}")


def save_bridge_config() -> None:
    BRIDGE_CONFIG_PATH.write_text(json.dumps(BRIDGE_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")


SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
_spotify_token: dict[str, Any] = {"value": None, "expires": 0.0}


def spotify_track_query(text: str) -> str | None:
    """Если запрос похож на Spotify-ссылку/URI — вернуть 'исполнитель - трек'."""
    import re as _re
    match = _re.search(r"(?:open\.spotify\.com/track/|spotify:track:)([A-Za-z0-9]+)", text)
    if not match or not (SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET):
        return None
    if time.time() >= _spotify_token["expires"]:
        response = _client.post(
            "https://accounts.spotify.com/api/token",
            data={"grant_type": "client_credentials"},
            headers={"authorization": "Basic " + base64.b64encode(
                f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()).decode()},
        )
        response.raise_for_status()
        token = response.json()
        _spotify_token["value"] = token["access_token"]
        _spotify_token["expires"] = time.time() + int(token.get("expires_in", 3600)) - 60
    meta = _client.get(
        f"https://api.spotify.com/v1/tracks/{match.group(1)}",
        headers={"authorization": "Bearer " + _spotify_token["value"]},
    ).json()
    artists = ", ".join(a["name"] for a in meta.get("artists", []))
    return (artists + " - " + meta.get("name", "")).strip(" -") or None


def resolve_track(query: str, source: str | None = None) -> dict[str, Any] | None:
    """Найти трек на выбранном источнике, вернуть {title, url, source}."""
    import shutil
    import subprocess

    source = (source or BRIDGE_CONFIG.get("source") or "youtube").lower()
    # спотифай-ссылка → метаданные, играет открытый источник
    resolved = spotify_track_query(query)
    if resolved:
        query, source = resolved, "youtube"
    if source == "spotify":
        source = "youtube"  # без прямой ссылки Spotify DRM не обойти

    search = {"soundcloud": "scsearch1:"}.get(source, "ytsearch1:")
    ytdlp = shutil.which("yt-dlp")
    if not ytdlp:
        return None
    cmd = [
        ytdlp,
        "--dump-json", "--no-playlist", "--no-warnings",
        "--default-search", search,
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
            "source": source,
        }
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Ручки
# --------------------------------------------------------------------------- #
def auth(request: Request) -> None:
    token = request.headers.get("x-voice-token", "") or request.query_params.get("token", "")
    if token != TOKEN:
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

    source = str(data.get("source") or intent.get("source") or "").lower() or None
    result: dict[str, Any] = {"intent": intent, "source": source or BRIDGE_CONFIG.get("source")}
    if intent.get("action") == "play":
        result["track"] = resolve_track(intent.get("query") or text, source)
    return JSONResponse(result)


@app.get("/api/bridge/config")
async def api_bridge_config(request: Request) -> JSONResponse:
    auth(request)
    return JSONResponse({
        "source": BRIDGE_CONFIG.get("source", "youtube"),
        "sources": ["youtube", "soundcloud", "spotify"],
        "spotify_configured": bool(SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET),
    })


@app.put("/api/bridge/config")
async def api_bridge_config_put(request: Request) -> JSONResponse:
    auth(request)
    data = await request.json()
    source = str(data.get("source", "")).lower()
    if source not in ("youtube", "soundcloud", "spotify"):
        raise HTTPException(status_code=400, detail="источник: youtube | soundcloud | spotify")
    BRIDGE_CONFIG["source"] = source
    save_bridge_config()
    return JSONResponse({"ok": True, "source": source})


@app.post("/api/tts")
async def api_tts(request: Request) -> JSONResponse:
    """Заготовка: ответ текстом, TTS пока нативный на колонке (aliced)."""
    auth(request)
    data = await request.json()
    return JSONResponse({"text": str(data.get("text", ""))[:500]})
