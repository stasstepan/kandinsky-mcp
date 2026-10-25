# Kandinsky API — сводная справка

Составлено по `GET /openapi.json` обоих хостов + текст PDF. Проверено: октябрь 2026.

## Общее

| | |
|---|---|
| Base URL | `https://kandinskyapi.sberdevices.ru` · альтернатива `https://kandinskyapi.ru` |
| Swagger UI | `…/docs` · спека `…/openapi.json` |
| Авторизация | `Authorization: Bearer <user_token>` |
| Content-Type | `application/json` |
| Формат | асинхронный: `POST /tasks/<route>` → `task_id` → poll → download |
| Health | `GET /health` → `{"status":"ok"}` (200) / 503 |

## Поток

```
POST /tasks/<route>  ──────────────►  200 {"task_id":"1842"}

GET /tasks/{task_id} ──── 202 {"status":"queued"}      … поллить
                     └─── 202 {"status":"processing"}
                     └─── 200 {"status":"done"}        → можно скачивать
                     └─── 200 {"status":"failed","error":"…"}

GET /tasks/{task_id}/result ───────►  200 binary (image/png | video/mp4)
```

## Время жизни результата

| Событие | Значение |
|---|---|
| Таймаут генерации | 45 мин → `failed` |
| Окно скачивания после `done` | **10 мин**, потом `failed` («unpicked») |
| Удаление медиа после успешного скачивания | через **60 с** |
| Повторный `GET /result` | **422** (одноразовая выдача) |
| Окно после первого `failed` | 1 мин |

## Коды ошибок

| Код | Тело / смысл |
|---|---|
| 400 | `task_id must be numeric` |
| 401/403 | нет/неверный Bearer |
| 404 | задача не найдена или чужая |
| 413 | `too large file` |
| 422 | `too big resolution` · `prompt too long Nchars>M max chars limit` · `text blocked by censor: …` · `input image blocked by censor: …` · `output blocked by censor: …` · `… censor service unavailable: GigaChat returned no response` · `Task is not ready (status=processing)` · `Task result was already delivered…` |
| 429 | `{"detail":{"code":"LIMIT_EXHAUSTED","type":"daily\|monthly\|concurrent\|rate",…}}` |
| 5xx | внутренняя ошибка, ретрай |

## Лимиты входа

| Что | Значение |
|---|---|
| Промпт | 2048 символов (`kandinsky6_i2i_distill`, `kandinsky-5.0-lorag`) |
| Входные картинки (i2i, i2v, avatar) | ≤ 25 МБ decoded, каждая сторона ≤ 2048 px |
| Superres вход | ≤ 4 МБ decoded, каждая сторона ≤ 1024 px |
| Квоты | daily / monthly на маршрут, минимальный интервал между запросами, лимит параллельных задач |

## Маршруты

Легенда: ✅ есть на хосте · ❌ нет. `sber` = `kandinskyapi.sberdevices.ru`, `ru` = `kandinskyapi.ru`.

### Изображения

| Маршрут | sber | ru | params | Выход |
|---|---|---|---|---|
| `k6-image-t2i` | ✅ | ✅ | `query`* · `resolution`* enum `1024x1024,768x768,768x1280,auto,1280x768` · `beautificator` enum `enabled,disabled,gigachat-max` | png |
| `k5-image-t2i` | ✅ | ✅ | `query`* · `resolution` enum `1024x1024,768x768,768x1280,1280x768,512x512` (=`1024x1024`) · `guidance` number≥0 (=3.5) · `use_original_prompt` bool (=false) | png |
| `k6-i2i` | ✅ | ✅ | `query`* · `image` array 1..3* · `beautificator` | png |
| `k6-kandishop-quantized` | ✅ | ❌ | `query`* · `image` array (опц.) · `beautificator` | png |
| `k6_superres` | ✅ | ✅ | `image` string* · `upscale` 2\|4* · `one_step_t` 0..1 (опц.) | png |

### Видео (без аудио)

| Маршрут | sber | ru | params |
|---|---|---|---|
| `k5_video_t2v_pro` | ✅ | ✅ | `query`* · `resolution`* enum `768x1280,1280x768` · `beautificator` |
| `k5_video_t2v_lite` | ✅ | ✅ | `query`* · `resolution`* enum `512x512,512x768,768x512` · `beautificator` |
| `k5-i2v-hd` | ✅ | ✅ | `query`* · `image` string* · `beautificator` |
| `k5-i2v-sd` | ✅ | ✅ | `query`* · `image` string* · `beautificator` |
| `k5-i2v-lite` | ✅ | ✅ | `query`* · `image` string* · `beautificator` |

### Видео с аудио

| Маршрут | sber | ru | params |
|---|---|---|---|
| `kandinsky6.5-preview-t2va` | ✅ | ❌ | `query`* · `seed` int · `duration` 5\|10\|15 (=5) · `rag` bool (=true) · `aspect_ration` `1:1,16:9,9:16` (=16:9) |
| `kandinsky6.5-preview-i2va` | ✅ | ❌ | то же + `images` array 1..3* |
| `k6_t2va_lite` | ❌ | ✅ | `query`* · `seed` int (=42) · `resolution` `512x512,512x768,768x512` (=768x512) · `video_duration` 1..10 (=5) · `beautificator` `enabled,disabled` |

### Аватар

| Маршрут | sber | ru | params |
|---|---|---|---|
| `giga_avatar` | ✅ | ✅ | `query`* · `image` base64* · `audio` base64 WAV mono* · `emotion` `anger_subtle,happiness_subtle,sadness_subtle` (опц.) |

\* — обязательный параметр.

## Ловушки спецификации

1. **Разделитель разрешения.** PDF пишет `"1280:768"`, схема — `"1280x768"`.
   Валидация в API — по enum, то есть правильно `x`. Принимать надо оба, наружу
   отправлять `x`.
2. **`aspect_ration`** — именно так, с опечаткой (не `aspect_ratio`). Ключ
   провалидируется строго, поэтому отправлять ровно как в схеме.
3. **`image` vs `images`.** То строка, то массив — от маршрута к маршруту.
   `k6-i2i` и `6.5-preview-i2va` — массив; `k5-i2v-*`, `giga_avatar`, `k6_superres` — строка.
4. **`additionalProperties: false`** во всех `params` — лишний ключ = 422.
5. **`censor`** по умолчанию `true`, и включённый цензор добавляет задачу
   ожидание воркера-цензора (медленнее, но безопаснее).
6. **`beautificator`** не падает при недоступности GigaChat — просто
   используется исходный промпт. А вот `censor` — падает (`censor service unavailable`).
7. **Список маршрутов у двух хостов разный** — см. таблицы; код должен брать его
   из `openapi.json`, а не хардкодить.
8. **`Content-Type` в `/result` не соответствует содержимому.** Живой прогон
   22.10.2026: заголовок `image/png`, а тело — JPEG (`ff d8 ff e0`, JFIF), файл
   1024×1024. Определять формат надо по сигнатуре байтов, иначе файл получит
   расширение `.png` и часть клиентов откажется его отображать.
9. **Результат отдаётся один раз.** Проверено: повторный `GET /result` → 422.
   Скачивать нужно сразу после `done`, укладываясь в 10 минут.
