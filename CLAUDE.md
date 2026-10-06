# arXiv Digest

Сервис по расписанию собирает препринты arXiv по темам, суммаризирует их локальной LLM (OpenAI-совместимый API), собирает PDF-дайджест на русском и рассылает его по email. Полное ТЗ — `arxiv-digest-prompt.md`.

## Архитектура

- `backend/app/core` — конфиг (`pydantic-settings`), логирование (`structlog`), БД, безопасность, ошибки, middleware.
- `backend/app/models` — модели SQLAlchemy 2.0, общие для API и Celery.
- `backend/app/api` — тонкие роутеры FastAPI; `backend/app/services` — бизнес-логика; `backend/app/tasks` — тонкие Celery-задачи.
- `frontend/` — админка (React + Vite + Mantine), nginx отдаёт статику и проксирует `/api` на backend.
- Compose: `postgres`, `rabbitmq`, `backend`, `worker` (очереди `default,mail`), `worker-llm` (очередь `llm`), `beat`, `frontend`; профиль `debug` — `flower`, `mailpit`.

## Команды

```bash
docker compose up -d --build                                           # весь стек, UI на http://localhost:8080
docker compose -f docker-compose.test.yml run --rm --build tests       # ruff + mypy + pytest
docker compose -f docker-compose.test.yml run --rm tests pytest tests/test_health.py   # один файл
docker compose -f docker-compose.test.yml run --rm tests \
  alembic revision --autogenerate -m "описание"                        # новая миграция (БД должна быть на head)
```

Backend-тесты и линтеры запускаются только в контейнере: там Python 3.12, WeasyPrint и шрифты.

## Соглашения

- Секреты — только из переменных окружения; в БД, логах и ответах API их нет. Новый секрет нужно добавить в `Settings.secret_values()`, а имя поля — в `SENSITIVE_KEYS` (`core/logging.py`).
- Все даты в БД — `timestamptz` в UTC; в таймзону из `TZ` переводятся только при отображении.
- FastAPI работает с БД через `asyncpg` (`get_session`), Celery и Alembic — через `psycopg` (`get_sync_sessionmaker`).
- Ошибки API — `{"detail": ..., "code": ...}`; бизнес-ошибки бросаются как `AppError` и наследники (`core/errors.py`).
- Логи — через `get_logger(__name__)`, события в snake_case, данные — именованными полями.
- Схема меняется только миграциями Alembic.

## Принятые решения

- **Enum-колонки хранятся строками** (`native_enum=False`), а не нативными типами PG: новое значение не требует `ALTER TYPE`.
- **Статус `cancelled` у `task_runs`** добавлен сверх ТЗ: отменённый вручную запуск не должен выглядеть как `failed`.
- **`task_runs.params`** (JSONB) добавлено сверх ТЗ: входные параметры запуска (период, `digest_id`, `force`).
- **`papers.full_text`** хранится в БД (TEXT, deferred), отдельных txt-файлов нет.
- **Один дайджест на период** (`UNIQUE(period_start, period_end)`): повторная сборка обновляет ту же запись.
- **`summaries.paper_id` уникален**: пересуммаризация заменяет резюме.
- **`updated_at` обновляется на стороне Python** (`onupdate=utcnow`), чтобы async-сессии не требовался повторный SELECT.
- **arXiv API вызывается по https** (`https://export.arxiv.org/api/query`): адрес из ТЗ с `http://` отвечает редиректом 301.
- **Пауза `ARXIV_REQUEST_DELAY_SECONDS`** выдерживается между любыми запросами к arXiv — и к API, и при скачивании PDF.
- **Статус пайплайна выводится из шагов** (`services/runs.py::_sync_parent`): ошибка или отмена шага переводит оставшиеся шаги в `skipped`, отдельной финализирующей задачи нет.
- **Ретраи на уровне задачи** — только для `RetryableError` и ошибок соединения с БД (до 3 раз, 60/120/240 с); запуск на это время возвращается в `queued`, причина — в `error`.
- **Повторный `collect`** дообрабатывает статьи, оставшиеся в `new` / `downloaded`, и те `failed`, у которых текст так и не был извлечён.
- **Список литературы** отрезается по первому заголовку References / Bibliography после первых 30% текста; приложения после него тоже отбрасываются.
- **Обновление сущностей — `PATCH`** с частичным телом (кроме singleton `PUT /smtp`).
- **Фильтр логов по уровню — «не ниже»**: `level=warning` возвращает warning, error и critical.
- **Пароли — `bcrypt` напрямую** (без passlib), JWT — `PyJWT`, HS256, срок жизни 12 часов.
- **Healthcheck beat** проверяет только, что процесс жив: у beat нет команды ping.
- **Баннеры Celery отключены** (`celery --quiet`), чтобы в stdout шёл только JSON.
- **Frontend-контейнер** — `nginx-unprivileged`, слушает 80 без root.
- **Шрифты дайджеста** — PT Sans / PT Serif (OFL) в `backend/assets/fonts/`.
