# Голосовой мост

Серверная часть голосового ассистента для Яндекс Станции (заменяет Алису).
Живёт на VPS, колонка общается с ним через YAM-плагин.

## Схема

```
«Алиса, дипсик, включи Кино»
  → aliced (штатный STT колонки) расшифровывает
  → YAM-плагин (триггер-фраза, permission commands) берёт transcript
  → POST https://voice.c4elovek.online/api/voice (токен в заголовке)
  → мост: LLM (OpenRouter, бесплатная модель) → намерение
  → если play: yt-dlp ищет трек → прямая ссылка потока
  → плагин играет её на колонке (alice.audio.play / aplay)
```

Wake word и распознавание — штатные, на колонке. На VPS только текст и музыкальный поиск.

## Запуск (когда будет VPS)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
# опционально: apt install yt-dlp (или pip install yt-dlp)
export VOICE_TOKEN=""            # пусто — сгенерируется и сохранится в voice-bridge-state.json
export LLM_PROVIDER="deepseek-web"  # deepseek-web | openrouter | none
export DEEPSEEK_TOKEN="..."      # токен сессии веб-чата (userToken из cookies), свой аккаунт
export LLM_KEY="sk-or-..."       # ключ OpenRouter — резерв
export LLM_MODEL="deepseek/deepseek-chat-v3-0324:free"
export VOICE_PROXY="socks5://..."  # опционально, для YouTube из РФ
uvicorn app:app --host 127.0.0.1 --port 8811
```

Перед ним Caddy с автоматическим TLS для `voice.c4elovek.online`:

```
voice.c4elovek.online {
    reverse_proxy 127.0.0.1:8811
}
```

## Ручки

- `GET /health` — без токена, для мониторов;
- `POST /api/voice` — `{"transcript": "включи Кино — Пачка сигарет"}`,
  заголовок `X-Voice-Token`. Ответ: `{"intent": {...}, "track": {...}}`;
- `POST /api/tts` — заготовка под будущий синтез речи.

## Проверка

```bash
curl -H "X-Voice-Token: $TOKEN" -H "content-type: application/json" \
     -d '{"transcript":"включи Кино — Пачка сигарет"}' \
     https://voice.c4elovek.online/api/voice
```

## Лицензия

[PolyForm Strict](LICENSE) (с правкой) — код открыт для просмотра и личного
использования. Копировать и пересылать можно только в исходном, немодифицированном
виде и с этим файлом лицензии. Переделывать, форкать и распространять изменённые
версии запрещено.

