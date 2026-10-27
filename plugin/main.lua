-- deepseek-bridge — голосовой плагин Станции: триггер-фраза → мост → действие
-- permissions: commands, audio, exec (network не нужен: HTTP идёт через exec/wget)

local BRIDGE_URL = "https://voice.c4elovek.online/api/voice"
local TOKEN = "ЗАМЕНИ_НА_ТОКЕН_ИЗ_VOICE_BRIDGE"

-- HTTP из плагина идёт через exec + busybox wget (плагиновый network запрещает
-- локальные и частные адреса, а exec-путь проверен в пакетах YAM)
local function http_post_json(url, body)
  local ok, out = pcall(alice.exec, "/bin/busybox", {
    "wget", "-qO-", "-T", "15",
    "--post-data=" .. body,
    "--header=content-type: application/json",
    "--header=X-Voice-Token: " .. TOKEN,
    url,
  })
  return ok, out
end

local function speak(text)
  -- TODO: озвучка — ТTS-директива через ipc.control или аудиофайл через audio.play
  print("[deepseek-bridge] " .. text)
end

local function handle(intent, track)
  local action = intent.action or "chat"
  if action == "play" and track and track.url then
    speak("включаю " .. (track.title or ""))
    alice.exec("/bin/busybox", { "wget", "-O", "/tmp/track.opus", "-T", "60", track.url })
    alice.audio.play("/tmp/track.opus")
  elseif action == "volume" then
    alice.volume.set(intent.volume or 50)
  else
    speak(intent.reply or "не поняла")
  end
end

alice.command.listen("дипсик", function(message)
  alice.command.cancel() -- молчим штатной Алисой, отвечаем сами
  local transcript = message.transcript or ""
  print("[deepseek-bridge] запрос: " .. transcript)

  local body = '{"transcript":' .. string.format("%q", transcript) .. "}"
  local ok, out = http_post_json(BRIDGE_URL, body, { "X-Voice-Token: " .. TOKEN })
  if not ok or not out or out == "" then
    speak("мост недоступен")
    return
  end
  -- минимальный парс ответа моста
  local intent = { action = "chat" }
  local track = nil
  local action = out:match('"action"%s*:%s*"([^"]+)"')
  if action then intent.action = action end
  local reply = out:match('"reply"%s*:%s*"([^"]*)"')
  if reply then intent.reply = reply end
  local turl = out:match('"url"%s*:%s*"([^"]+)"')
  local ttitle = out:match('"title"%s*:%s*"([^"]*)"')
  if turl then track = { url = turl, title = ttitle } end

  handle(intent, track)
end)
