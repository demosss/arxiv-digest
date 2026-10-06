# Задание для Claude Code: сервис «arXiv Digest»

## Роль и цель

Ты — senior full-stack разработчик. Реализуй с нуля production-ready веб-приложение «arXiv Digest».

Приложение по расписанию выполняет четыре шага:
1. Собирает свежие препринты с arxiv.org по заданным темам (ключевым словам).
2. Суммаризирует полный текст каждой статьи с помощью локальной LLM (OpenAI-совместимый API).
3. Формирует PDF-дайджест на русском языке.
4. Рассылает дайджест на список email-адресов.

Управление сервисом идёт через панель администратора.

Работай поэтапно (см. раздел «Этапы»). Перед началом составь план в plan mode и покажи его мне. После каждого этапа:
- проверь критерии приёмки;
- запусти тесты и линтер;
- сделай git-коммит с осмысленным сообщением;
- кратко отчитайся, что сделано.

Не переходи к следующему этапу, пока текущий не работает.

---

## Технологический стек

| Слой | Технологии |
|---|---|
| Backend | Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.0, Alembic |
| БД | PostgreSQL 16. В FastAPI — async-драйвер `asyncpg`, в Celery — sync-драйвер `psycopg` (v3). Модели общие |
| Фоновые задачи | Celery 5 + RabbitMQ (брокер). Результаты задач хранятся в своей таблице `task_runs`, отдельный result backend не нужен |
| LLM | Пакет `openai` (Python SDK) с `base_url`, указывающим на локальную модель (vLLM / Ollama / LM Studio и т.п.) |
| Парсинг arXiv | `httpx` + `feedparser` (Atom API arXiv) |
| Извлечение текста из PDF | `PyMuPDF` (fitz) |
| PDF-дайджест | `Jinja2` (HTML-шаблон) + `WeasyPrint` |
| Почта | `smtplib` из стандартной библиотеки (воркеры синхронные) |
| Расписание | `croniter` + периодическая задача Celery Beat (см. раздел «Планировщик») |
| Frontend | React 18 + TypeScript + Vite, React Router, TanStack Query, Mantine UI, axios, react-hook-form + zod |
| Развёртывание | Docker + docker compose |
| Качество кода | ruff, mypy (backend); eslint + prettier, `tsc --noEmit` (frontend); pytest |

---

## Структура репозитория

```
arxiv-digest/
├── docker-compose.yml
├── .env.example
├── README.md
├── CLAUDE.md                 # краткое описание архитектуры и соглашений для будущих сессий
├── backend/
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── alembic/ , alembic.ini
│   ├── app/
│   │   ├── main.py           # FastAPI app
│   │   ├── core/             # config (pydantic-settings), logging, security, db
│   │   ├── models/           # SQLAlchemy модели
│   │   ├── schemas/          # Pydantic схемы
│   │   ├── api/              # роутеры: auth, topics, recipients, schedules, smtp, runs, papers, digests, logs, dashboard
│   │   ├── services/         # arxiv_client, pdf_text, llm_summarizer, digest_builder, mailer, scheduler
│   │   ├── tasks/            # celery_app.py, pipeline.py, scheduler_tick.py
│   │   └── templates/        # digest.html.j2, email.html.j2
│   ├── assets/fonts/         # TTF-шрифты с кириллицей
│   └── tests/
└── frontend/
    ├── Dockerfile            # multi-stage: build → nginx
    ├── nginx.conf            # отдаёт статику, проксирует /api → backend
    └── src/
```

---

## Конфигурация и секреты (`.env`)

Все секреты задаются **только через переменные окружения**. В коде, в БД и в git секретов нет. Создай `.env.example` со всеми переменными и комментариями. Добавь `.env` в `.gitignore`.

```
# --- Общие ---
APP_ENV=production
TZ=Europe/Moscow
LOG_LEVEL=INFO
LOG_FORMAT=json                 # json | console
SECRET_KEY=change-me            # для JWT

# --- Админ (создаётся при первом старте, если не существует) ---
ADMIN_USERNAME=admin
ADMIN_PASSWORD=change-me

# --- PostgreSQL ---
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
POSTGRES_DB=arxiv_digest
POSTGRES_USER=arxiv
POSTGRES_PASSWORD=change-me

# --- RabbitMQ ---
RABBITMQ_HOST=rabbitmq
RABBITMQ_USER=arxiv
RABBITMQ_PASSWORD=change-me

# --- LLM (OpenAI-совместимый API локальной модели) ---
LLM_BASE_URL=http://host.docker.internal:8000/v1
LLM_API_KEY=not-needed          # секрет; для локальной модели может быть заглушкой
LLM_MODEL=qwen2.5-14b-instruct
LLM_TEMPERATURE=0.2
LLM_MAX_TOKENS=1024             # макс. токенов ответа
LLM_CONTEXT_CHARS=24000         # размер чанка входного текста в символах
LLM_TIMEOUT_SECONDS=300
LLM_CONCURRENCY=1               # параллельность очереди llm

# --- SMTP: только секрет ---
SMTP_PASSWORD=change-me

# --- arXiv ---
ARXIV_REQUEST_DELAY_SECONDS=3
ARXIV_MAX_RESULTS_PER_TOPIC=200
ARXIV_USER_AGENT=arxiv-digest/1.0 (mailto:admin@example.com)

# --- Хранилище ---
DATA_DIR=/data                  # PDF статей и дайджестов, лог-файлы
```

**Как разделены SMTP-настройки между админкой и `.env`.**
- В панели администратора редактируются несекретные параметры: host, port, username, from_email, from_name, режим шифрования (`none` / `starttls` / `ssl`), timeout. Они хранятся в БД.
- Пароль SMTP берётся только из `SMTP_PASSWORD` в `.env`.
- В админке показывай только статус «пароль задан / не задан». Значение пароля не показывай и не принимай через API.

Конфиг читается через `pydantic-settings`. При старте выполняется валидация: если не задан обязательный параметр, сервис падает с понятной ошибкой.

---

## Модель данных

Все таблицы содержат `id`, `created_at` и `updated_at`, где это уместно. Все изменения схемы делаются через миграции Alembic.

- **admin_users** — `username` (unique), `password_hash` (bcrypt), `is_active`.
- **topics** — `name`, `keywords` (массив строк), `arxiv_categories` (массив строк, опционально, например `cs.CL`), `is_active`.
- **recipients** — `email` (unique, валидация), `name` (опционально), `is_active`.
- **schedules** — расписания запусков:
  - `name`;
  - `task_type` — enum: `full_pipeline`, `collect`, `summarize`, `build_digest`, `send_digest`;
  - `cron_expression` — валидируется через croniter;
  - `timezone` (по умолчанию `Europe/Moscow`);
  - `is_active`, `last_fired_at`, `next_fire_at`.
- **smtp_settings** — singleton (одна строка): `host`, `port`, `username`, `from_email`, `from_name`, `security` (enum), `timeout_seconds`.
- **papers**:
  - `arxiv_id` (unique, без версии) и `version`;
  - `title`, `authors` (массив), `abstract`, `categories`;
  - `published_at`, `updated_at_arxiv`, `pdf_url`, `abs_url`;
  - `pdf_path`, `full_text` (или путь к txt), `text_chars`;
  - `status` — enum: `new`, `downloaded`, `text_extracted`, `summarized`, `failed`;
  - `error`.
- **paper_topics** — связь многие-ко-многим: какие темы и какие ключевые слова сматчили статью.
- **summaries** — `paper_id`, `text_ru`, `model`, `prompt_tokens`, `completion_tokens`, `duration_ms`, `chunks_count`.
- **digests**:
  - `period_start`, `period_end` (UTC);
  - `pdf_path`, `papers_count`;
  - `status` — enum: `building`, `ready`, `sent`, `partially_sent`, `failed`, `empty`.
- **digest_papers** — состав дайджеста, порядок статей.
- **digest_deliveries** — `digest_id`, `email`, `status` (`pending`, `sent`, `failed`), `attempts`, `error`, `sent_at`.
- **task_runs** — каждый запуск задачи или шага пайплайна:
  - `task_type`;
  - `trigger` — `schedule` / `manual`;
  - `schedule_id` (nullable), `parent_run_id` (шаги пайплайна ссылаются на родительский запуск);
  - `celery_task_id`;
  - `status` — `queued`, `running`, `success`, `failed`, `skipped`;
  - `started_at`, `finished_at`, `duration_ms`;
  - `stats` (JSONB, например «найдено 42, новых 30, ошибок 1»), `error`, `traceback`.
- **task_logs** — `run_id`, `ts`, `level`, `logger`, `message`, `extra` (JSONB). Индекс по `(run_id, ts)`.

---

## Логирование (обязательно, «везде где можно»)

1. **Единая настройка** в `app/core/logging.py`. Используй `structlog` поверх stdlib logging и применяй её в FastAPI, Celery worker и Celery beat.
   - Формат JSON в production, человекочитаемый при `LOG_FORMAT=console`.
   - Логи пишутся в stdout и в файл с ротацией (`RotatingFileHandler`, `DATA_DIR/logs/*.log`, 10 MB × 5).
2. **Контекст в каждой записи:** `service` (api / worker / beat), `request_id` (для HTTP), `run_id` и `task_type` (для задач), `paper_id` / `arxiv_id`, где применимо.
3. **HTTP middleware.** Генерирует `X-Request-ID` и логирует метод, путь, статус, длительность и пользователя. Тела запросов с паролями не логирует.
4. **Celery.**
   - Сигналы `task_prerun`, `task_postrun`, `task_failure`, `task_retry` логируются.
   - Отдельный logging handler во время выполнения задачи пишет записи уровня INFO и выше в таблицу `task_logs`, привязанные к `run_id`. Запись делается батчами или в отдельной сессии, чтобы ошибка логирования не роняла задачу.
5. **Что логировать по сервисам.**
   - **arXiv:** URL запроса (без секретов), число результатов, пагинация, задержки, ретраи, HTTP-ошибки.
   - **Скачивание PDF:** размер, время.
   - **Извлечение текста:** число страниц и символов.
   - **LLM:** модель, число чанков, токены, длительность каждого вызова, ретраи, таймауты. Полные промпты и ответы — только на уровне DEBUG.
   - **PDF-дайджест:** число статей, размер файла, время генерации.
   - **SMTP:** подключение, каждый получатель, результат, ошибки.
   - **Планировщик:** каждый тик (на DEBUG), каждое срабатывание расписания (на INFO), пропуски из-за дедупликации.
   - **CRUD в админке:** аудит-лог, кто что создал, изменил или удалил.
6. **Маскирование секретов.** Пароли, `LLM_API_KEY`, `SMTP_PASSWORD` и JWT никогда не попадают в логи. Добавь процессор structlog, который маскирует поля по списку имён.

---

## Шаги пайплайна (Celery-задачи)

Каждый шаг — отдельная задача, идемпотентная и пригодная для запуска по отдельности. `full_pipeline` запускает Celery `chain` из четырёх шагов, передавая между ними `digest_id` и `period`.

Очереди:
- `default` — сбор, PDF;
- `llm` — суммаризация, отдельный воркер с `concurrency=LLM_CONCURRENCY`;
- `mail` — рассылка.

Для всех шагов:
- ретраи с экспоненциальной задержкой на сетевых ошибках;
- `acks_late=True`;
- разумные `soft_time_limit` / `time_limit`.

### 1. `collect` — сбор препринтов

- **Период.** По умолчанию берутся прошедшие сутки: предыдущий календарный день в UTC, `[D 00:00, D 23:59:59]`. Параметры `period_start` / `period_end` можно передать вручную.
- **Запрос.** Для каждой активной темы строится запрос к `http://export.arxiv.org/api/query`:
  - ключевые слова объединяются через `OR`, фразы берутся в кавычки (`all:"large language model"`);
  - если заданы `arxiv_categories`, добавляется `AND (cat:cs.CL OR cat:cs.AI)`;
  - фильтр периода: `submittedDate:[YYYYMMDDHHMM TO YYYYMMDDHHMM]`;
  - сортировка `sortBy=submittedDate&sortOrder=descending`;
  - пагинация по 100;
  - пауза `ARXIV_REQUEST_DELAY_SECONDS` между запросами;
  - корректный User-Agent.
- **Обработка результатов.**
  - Сохраняются новые статьи. Дедупликация по `arxiv_id` без версии; если статья пришла по нескольким темам, создаются связи в `paper_topics`.
  - Для каждой новой статьи скачивается PDF в `DATA_DIR/papers/{arxiv_id}.pdf` с ретраями. Затем PyMuPDF извлекает текст.
  - Текст очищается: дефисные переносы, повторяющиеся колонтитулы, лишние пробелы. Раздел References / Bibliography отрезается эвристикой.
- **Статистика в `task_runs.stats`:** по каждой теме найдено / новых / дублей / ошибок скачивания.
- **Пустой период** (выходные arXiv) — это не ошибка. Статус `success`, в stats `papers_found=0`.

### 2. `summarize` — суммаризация полного текста через LLM

- Берутся статьи со статусом `text_extracted` (или переданные `paper_ids`).
- Клиент: `openai.OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY, timeout=LLM_TIMEOUT_SECONDS)`.
- **Map-reduce по полному тексту.**
  - Текст режется на чанки по `LLM_CONTEXT_CHARS` с перекрытием около 5%, по границам абзацев.
  - Если чанк один, суммаризация выполняется одним вызовом.
  - Иначе каждый чанк сжимается в конспект (map), затем из конспектов, названия и аннотации собирается итоговое резюме (reduce).
- **Итоговое резюме:** на русском языке, 150–250 слов, структура:
  - **Задача** — какую проблему решают;
  - **Метод** — ключевая идея;
  - **Результаты** — главные цифры и выводы;
  - **Значимость** — почему это интересно.

  Без выдуманных фактов. Термины можно оставлять на английском в скобках.
- Промпты хранятся в отдельном модуле `services/prompts.py`, чтобы их было легко править.
- Ответ валидируется: не пустой, без мусора вроде «As an AI…». При невалидном ответе — один повтор.
- Сохраняются `summaries` с токенами и длительностью. Статус статьи → `summarized`; при ошибке → `failed` с текстом ошибки, обработка остальных статей продолжается.
- Добавь healthcheck LLM: эндпоинт `GET /api/llm/health` делает `models.list()` или короткий тестовый запрос.

### 3. `build_digest` — PDF-дайджест на русском

- В дайджест попадают статьи за период со статусом `summarized`, сгруппированные по темам.
- Если статья относится к нескольким темам, в документе она выводится один раз, в первой теме, а в остальных — со ссылкой на неё.
- **HTML-шаблон Jinja2 → WeasyPrint.** Содержимое документа:
  - титульный блок: «Дайджест препринтов arXiv», период, дата формирования, число статей;
  - оглавление по темам;
  - для каждой статьи: название (ссылка на abs), авторы (первые 5 + «и др.»), категории, дата, резюме;
  - номера страниц в колонтитуле.
- **Шрифты с кириллицей кладутся в контейнер.**
  - В `backend/Dockerfile` установи `fonts-dejavu-core` и `fonts-noto-core`, а также системные зависимости WeasyPrint (pango, harfbuzz и т.д.).
  - Дополнительно положи TTF-файлы (например, PT Sans / PT Serif или Noto Sans — свободные лицензии) в `backend/assets/fonts/` и подключи их через `@font-face` с локальными путями.
  - Выполни `fc-cache -f`.
  - Тест проверяет, что сгенерированный PDF содержит встроенный шрифт и кириллица извлекается обратно.
- PDF сохраняется в `DATA_DIR/digests/digest_YYYY-MM-DD.pdf`, создаётся запись в `digests`.
- Если статей нет, создаётся дайджест со статусом `empty`. Рассылка при этом по умолчанию пропускается; это поведение задаётся флагом в настройках.

### 4. `send_digest` — рассылка

- Берётся указанный (или последний `ready`) дайджест и активные получатели.
- Используются SMTP-настройки из БД и `SMTP_PASSWORD` из окружения. Поддерживаются `none` / `starttls` / `ssl`.
- Письмо в формате HTML + текст. Тема: «Дайджест arXiv за DD.MM.YYYY». Внутри — краткий список названий статей. PDF прикреплён вложением.
- Каждому получателю отправляется отдельное письмо: адреса не раскрываются, ошибка одного не ломает остальных.
- Для каждого получателя создаётся запись в `digest_deliveries`, до 3 попыток с задержкой.
- Итоговый статус дайджеста: `sent` / `partially_sent` / `failed`.
- Защита от повторной отправки: тому же получателю тот же дайджест повторно не отправляется, если не передан `force=True`.

---

## Планировщик (вариант «тик раз в минуту»)

- Celery Beat содержит **одну** статическую периодическую задачу `scheduler.tick` с интервалом 60 секунд.
- Алгоритм `tick`:
  1. Выбрать активные `schedules` с блокировкой `SELECT … FOR UPDATE SKIP LOCKED`.
  2. Для каждого расписания вычислить через `croniter` в его `timezone` последнее время срабатывания `≤ now`.
  3. Если оно больше `last_fired_at`, создать `task_runs` (`trigger=schedule`), поставить в очередь задачу нужного `task_type`, обновить `last_fired_at` и `next_fire_at`.
  4. Пропущенные срабатывания (например, сервис лежал) не догоняются пачкой: запускается максимум одно последнее, и это пишется в лог.
- Защита от параллельных запусков пайплайна. Если `full_pipeline` уже `running`, новый запуск получает статус `skipped` с причиной в логе. Используй advisory lock PostgreSQL.
- При создании или изменении расписания API пересчитывает `next_fire_at` и возвращает его. В UI показывается ближайшее время запуска.

---

## REST API (`/api`)

- Все эндпоинты, кроме `POST /api/auth/login` и `GET /api/health`, требуют JWT (Bearer). Срок жизни токена 12 часов.
- Ошибки возвращаются в едином формате `{ "detail": ..., "code": ... }`.
- Списки поддерживают пагинацию (`limit`, `offset`) и фильтры.
- OpenAPI доступен на `/api/docs`.

| Ресурс | Эндпоинты |
|---|---|
| Auth | `POST /auth/login`, `GET /auth/me`, `POST /auth/change-password` |
| Health | `GET /health` (БД, RabbitMQ), `GET /llm/health` |
| Topics | CRUD `/topics` |
| Recipients | CRUD `/recipients` |
| Schedules | CRUD `/schedules`, `POST /schedules/validate-cron` (возвращает 5 ближайших запусков) |
| SMTP | `GET /smtp`, `PUT /smtp`, `POST /smtp/test` (отправить тестовое письмо на указанный адрес) |
| Runs | `GET /runs` (фильтры: тип, статус, даты), `GET /runs/{id}` (с дочерними шагами), `POST /runs` (ручной запуск: `task_type` + опц. период), `POST /runs/{id}/cancel` |
| Logs | `GET /runs/{id}/logs` (фильтр по уровню), `GET /logs` (общий поиск по `task_logs`) |
| Papers | `GET /papers` (фильтры: тема, статус, период, поиск по названию), `GET /papers/{id}` (с резюме), `POST /papers/{id}/resummarize` |
| Digests | `GET /digests`, `GET /digests/{id}` (состав + доставки), `GET /digests/{id}/pdf` (скачивание), `POST /digests/{id}/resend` |
| Dashboard | `GET /dashboard` (последние запуски, ближайшие расписания, счётчики за 7 дней, статус LLM/SMTP) |

---

## Панель администратора (frontend)

Интерфейс на русском языке. Layout с боковым меню. Используй Mantine. Ошибки API показываются через уведомления, удаление подтверждается модальным окном.

Страницы:
1. **Вход.**
2. **Дашборд** — карточки «статей за 7 дней», «дайджестов отправлено», статус LLM и SMTP (зелёный / красный), ближайшие запуски по расписаниям, последние 10 запусков. Кнопка «Запустить пайплайн сейчас».
3. **Темы** — таблица; создание и редактирование в модалке: название, ключевые слова (TagsInput), категории arXiv (MultiSelect с популярными категориями + свободный ввод), активность; удаление.
4. **Получатели** — таблица; добавление, редактирование, удаление, переключатель активности; валидация email.
5. **Расписания** — таблица с колонками «тип задачи», «cron», «таймзона», «следующий запуск», «активно».
   - В форме есть простой режим: выбрать время и дни недели, cron генерируется автоматически.
   - Есть продвинутый режим: ввод cron вручную с live-превью 5 ближайших запусков через `/schedules/validate-cron`.
6. **Настройки SMTP** — форма с параметрами, индикатор «Пароль задан в .env: да/нет», кнопка «Отправить тестовое письмо».
7. **Запуски** — список с фильтрами. Карточка запуска показывает шаги пайплайна, статистику, ошибку с traceback и **просмотр логов** с фильтром по уровню и автообновлением, пока статус `running`. Также ручной запуск любого шага с выбором периода.
8. **Статьи** — список с фильтрами; карточка: метаданные, резюме, ссылка на arXiv, кнопка «пересуммаризировать».
9. **Дайджесты** — список; скачивание PDF; статусы доставки по получателям; кнопка «отправить повторно».

Токен хранится в памяти и в `localStorage`. При ответе 401 происходит редирект на вход. Типы API описаны вручную в `src/api/types.ts` (или генерируются из OpenAPI — на твой выбор, но последовательно).

---

## Docker compose

Сервисы:
- `postgres` (16-alpine, volume, healthcheck `pg_isready`);
- `rabbitmq` (3-management-alpine, healthcheck);
- `backend`:
  - при старте выполняет `alembic upgrade head`, затем создаёт админа из `.env`, если его нет;
  - запускается через uvicorn;
  - healthcheck на `/api/health`;
- `worker` — Celery, очереди `default,mail`;
- `worker-llm` — Celery, очередь `llm`, concurrency из `.env`;
- `beat` — Celery Beat; единственный экземпляр, schedule-файл в volume;
- `frontend` — nginx на порту 80 (наружу `8080`), проксирует `/api` → `backend:8000`;
- опционально `flower` в профиле `debug`.

Требования:
- общий volume `data` для `DATA_DIR`, смонтирован в backend и воркеры;
- `depends_on` с `condition: service_healthy`;
- у backend и воркеров `extra_hosts: ["host.docker.internal:host-gateway"]`, чтобы контейнеры достучались до локальной LLM на хосте;
- `restart: unless-stopped`;
- backend, worker и beat используют один образ с разными командами;
- контейнеры работают не от root.

---

## Тесты

pytest, внешние сервисы замоканы:
- построение поискового запроса к arXiv по теме, парсинг Atom-ответа (фикстура с сохранённым XML), дедупликация;
- очистка текста и отрезание References;
- чанкинг и map-reduce суммаризации (мок OpenAI-клиента);
- `scheduler.tick`: срабатывание, отсутствие повторного запуска, пропущенные срабатывания, таймзоны;
- генерация PDF: файл создаётся, кириллица извлекается, шрифт встроен;
- mailer: мок SMTP, частичные ошибки → `partially_sent`, без повторной отправки;
- API: авторизация, CRUD тем, получателей и расписаний, `PUT /smtp` не принимает пароль;
- маскирование секретов в логах.

Тесты БД гоняются на отдельной PostgreSQL (сервис в `docker-compose.test.yml` или testcontainers).

---

## Этапы и критерии приёмки

1. **Каркас.** Структура репозитория, docker compose, конфиг, логирование, модели, первая миграция, `/api/health`.
   - *Приёмка:* `docker compose up -d` поднимает все сервисы в состоянии healthy; логи в JSON с полем `service`.
2. **Auth + CRUD.** Темы, получатели, расписания, SMTP-настройки, тесты API.
   - *Приёмка:* все CRUD-эндпоинты работают через `/api/docs`.
3. **Сбор arXiv** (задача `collect` + ручной запуск через API + `task_runs` / `task_logs`).
   - *Приёмка:* ручной запуск собирает реальные статьи за вчера по тестовой теме, PDF скачаны, текст извлечён, логи видны через `/api/runs/{id}/logs`.
4. **Суммаризация.**
   - *Приёмка:* с запущенной локальной моделью статьи получают русские резюме; при недоступной LLM запуск падает с понятной ошибкой, статьи помечены `failed`, логи объясняют причину.
5. **PDF-дайджест.**
   - *Приёмка:* сгенерированный PDF корректно показывает кириллицу, содержит оглавление и ссылки.
6. **Рассылка.**
   - *Приёмка:* тестовое письмо и дайджест приходят (для локальной проверки добавь в профиль `debug` сервис MailHog / Mailpit).
7. **Планировщик + full_pipeline.**
   - *Приёмка:* расписание «каждые 5 минут» запускает пайплайн ровно один раз за интервал; параллельный запуск получает `skipped`.
8. **Frontend.** Все страницы из раздела «Панель администратора».
   - *Приёмка:* весь сценарий проходится через UI — завести тему, получателя, SMTP, расписание; запустить вручную; посмотреть логи; скачать PDF.
9. **Финал.** README (запуск, настройка `.env`, подключение локальной LLM, типовые проблемы), `CLAUDE.md`, прогон линтеров и всех тестов.

---

## Общие правила

- Код типизирован, без заглушек `TODO` в итоговой версии. Бизнес-логика живёт в `services/`; роутеры и задачи тонкие.
- Все даты в БД хранятся в UTC (`timestamptz`); в UI и в PDF они отображаются в таймзоне из настроек.
- Никаких секретов в коде, логах, ответах API и в git.
- Если требование неоднозначно — выбери разумный вариант, зафиксируй его в `CLAUDE.md` в разделе «Принятые решения» и сообщи мне в отчёте по этапу.
