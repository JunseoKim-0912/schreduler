# Schreduler

한국어: [README.ko.md](README.ko.md)

Schreduler is a scheduling backend built around one idea: a calendar shouldn't just record your plans, it should help you keep them. You tell it what you want in plain language, a tool-using assistant drafts the change, and nothing is saved until you confirm. Start/end reminders, completion check-ins, a persona that talks through your day, and points with streaks keep you on track.

This repository is the **backend (Python / FastAPI)** plus a small dependency-free web client served from the same server. A mobile client talks to the same REST API.

- Design document (Korean): [`docs/기획보고서.md`](docs/기획보고서.md)
- Assistant design and evaluation notes (Korean): [`docs/assistant_design.md`](docs/assistant_design.md)
- API overview for client developers (Korean): [`docs/api_overview.md`](docs/api_overview.md)
- Postman collection: [`docs/postman/Schreduler.postman_collection.json`](docs/postman/Schreduler.postman_collection.json)

## Features

| Feature | What it does |
|---|---|
| **Natural-language assistant** | Add, change or delete events and repeat periods by just saying it ("ECE360 Lab every other Tuesday 9–12", "move today's quiz from 5 to 6"). An LLM agent looks up existing events with tools and fills in missing details with sensible guesses, marked as *guessed* on the card. |
| **Confirmation cards & undo** | Every change arrives as a card showing what will happen, how many occurrences it touches, and warnings (crosses midnight, already past, a similar event exists, …). It's saved only when you press **Create** or reply "ok", and every saved change can be undone. Changing one occurrence of a repeating event detaches just that occurrence. |
| **Weekly calendar** | A week view (day view on phones) with importance colors, deadlines pinned on top, completion and deletion right from the event popover. |
| **Deadlines & tasks** | Deadline-type events double as a to-do list: due-date reminders, overdue markers, mark as done. |
| **Travel time** | Give an event a location and a travel block is added in front of it automatically. |
| **Points & streaks** | Completed events earn their importance in points; finishing every event several days in a row multiplies them (3 days ×1.1, 7 days ×1.25, 14 days ×1.5). |
| **Persona check-ins** | Pick a character and have an evening check-in about the day. Missed events come up first; if you log why you missed something, the persona replies in its own voice. |
| **Daily AI budget** | Every LLM call is logged with its tokens and cost. A per-user and an app-wide daily cap (reset at midnight in `APP_TIMEZONE`) stop LLM calls with HTTP 429; everything that doesn't need the LLM keeps working. The web UI shows "Today's AI usage $0.12 / $1.00" under the chat inputs. |
| **Korean / English** | The web UI starts in English with an **Eng \| Kor** switch. Notifications, cards and labels follow the screen language; chat replies follow the language of your last message. |

## Tech stack

Python 3.12+ · FastAPI · SQLAlchemy 2.x · Alembic · SQLite (dev) / PostgreSQL · APScheduler · OpenAI Responses & Chat Completions APIs · Firebase Cloud Messaging · Telegram Bot API · pytest · plain HTML/CSS/JS front end (no build step)

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env                   # then fill in values; LLM_API_KEY is needed for the assistant and personas

alembic upgrade head                   # create / migrate the database (default: ./schreduler.db)
python -m app.scripts.seed             # create a test user — prints its id
python -m app.scripts.seed_personas    # load the default personas (app/scripts/personas_seed_data.json)

uvicorn app.main:app --reload
```

Then open:

- Web app: <http://localhost:8000/app/> — enter the user id printed by the seed script in **User ID** (top right)
- Swagger UI: <http://localhost:8000/docs>
- Health check: <http://localhost:8000/health>

The web app is served by the same FastAPI process from `frontend/`, so there is nothing to build and no CORS to configure.

### Docker

```bash
docker compose up --build                                                        # SQLite, stored in a named volume
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up --build    # PostgreSQL
```

The container runs `alembic upgrade head` on start. The scheduler lives inside the server process, so it runs with a single worker.

## Configuration

Settings come from environment variables or a `.env` file. [`.env.example`](.env.example) documents each one; never commit `.env`.

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./schreduler.db` | SQLAlchemy URL. For PostgreSQL: `postgresql+psycopg://user:pw@host:5432/db` |
| `LLM_API_KEY` | *(none)* | OpenAI API key. Without it, LLM endpoints return 500 |
| `LLM_MODEL` | `gpt-5.6-luna` | Model for persona chat, check-ins and missed-event feedback |
| `ASSISTANT_MODEL` | `gpt-5.6-luna` | Model for the scheduling assistant (Responses API) |
| `ASSISTANT_REASONING_EFFORT` | `medium` | `reasoning.effort` for the assistant; validated per model at startup |
| `LLM_DAILY_BUDGET_PER_USER_USD` | `1.00` | Daily LLM spend cap per user (USD), reset at `APP_TIMEZONE` midnight |
| `LLM_DAILY_BUDGET_TOTAL_USD` | `5.00` | Daily LLM spend cap for all users and scripts together |
| `LLM_DAILY_BUDGET_ADMIN_USD` | *(none)* | Cap for users with `is_admin`; without it, admins get the per-user cap |
| `APP_TIMEZONE` | `America/Toronto` | IANA time zone used for "today", weekdays and reminders |
| `FIREBASE_CREDENTIALS_PATH` | *(none)* | Path to an FCM service-account JSON. Without it, push notifications are only logged |
| `TELEGRAM_BOT_TOKEN` | *(none)* | Bot token for escalation messages. Without it, they are only logged |
| `LOG_LEVEL` | `INFO` | Application log level |
| `ENVIRONMENT` | `development` | Environment name |

Docker only: `DOCKER_DATABASE_URL` (database URL inside the container, used instead of `DATABASE_URL`), `DOCKER_FIREBASE_CREDENTIALS_PATH`, `API_PORT` (default `8000`), and `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` (all default to `schreduler`, for development).

## Tests and evaluation

```bash
python -m pytest                         # everything under tests/ (also runs the JS tests if Node is installed)
python -m pytest --cov=app               # with coverage
node --test tests/frontend/*.test.mjs    # front-end unit tests only
```

Tests use in-memory SQLite and scripted fake LLM responses, so they never call a real API.

The assistant also has an evaluation set of real-world requests (`tests/assistant_eval/cases.yaml`). It calls the real API, so it costs money and is kept out of pytest:

```bash
python -m app.scripts.eval_assistant --effort medium
python -m app.scripts.eval_assistant --summarize "tests/assistant_eval/results/*.json"   # compare saved runs
```

Each run prints pass rates by category, LLM calls, tokens, latency and cost per turn, and saves a JSON report to `tests/assistant_eval/results/`.

Other scripts:

| Command | Purpose |
|---|---|
| `python -m app.scripts.export_postman` | Regenerate the Postman collection from the OpenAPI spec (run after API changes) |
| `python -m app.scripts.compare_prompt_cache --task daily_checkin --repeat 5` | Compare input tokens with and without prompt caching (real API calls) |
| `python -m app.scripts.usage_report --days 7` | LLM cost table by day, user and feature (from `llm_usage_logs`) |

## Project structure

```
app/
├── main.py              # FastAPI app: routers, exception handlers, scheduled jobs
├── frontend_serving.py  # serves frontend/ with cache-busting asset URLs
├── api/                 # routers (endpoints)
├── models/              # SQLAlchemy models
├── schemas/             # Pydantic request/response schemas
├── services/
│   ├── assistant/       # tool-using scheduling assistant: agent loop, tools, drafts, prompt, execution
│   ├── draft_rules.py   # shared validation and normalization for drafts (RRULEs, previews, time strings)
│   ├── llm_client.py    # Responses and Chat Completions clients, prompt caching, usage logging
│   ├── llm_usage.py     # per-call usage log and the daily budget check
│   ├── llm_pricing.py   # per-model token prices (shared with the evaluation script)
│   └── …                # events, ranges, undo history, notifications, points, check-ins
├── child_events/        # travel-time and prep sub-events
├── core/                # settings, DB, scheduler, auth, errors, clock, OpenAPI metadata
├── i18n/                # Korean/English messages, notifications, category labels
└── scripts/             # seeding, evaluation, Postman export
frontend/                # static web client (HTML/CSS/JS, en/ko dictionaries in i18n.js)
alembic/                 # database migrations
tests/                   # pytest and Node tests, assistant evaluation set
docs/                    # design docs, API overview, Postman collection
```

## Known limitations

- **No real authentication yet.** Requests identify the user with an `X-User-Id` header; there is no sign-up or user-management API (use the seed script).
- **No device-token registration.** Without an FCM token endpoint, push notifications are logged rather than delivered.
- **One time zone per server.** Everything follows `APP_TIMEZONE`; per-user time zones aren't supported yet.
- **Reminder jobs live in memory.** They are re-registered from the database on startup, but a single worker is required.
- **The assistant is probabilistic.** It is evaluated against a fixed set of requests (above 90% in recent runs with the default model) and every change needs confirmation, but it can still misread a request.

## License

[MIT](LICENSE) © 2026 Junseo Kim
