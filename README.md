# Bambi Knowledge Agent

Local Dockerized knowledge agent for Bambi course and FAQ data.

## Quick start

1. Set `OPENAI_API_KEY` in `.env`.
2. Run `docker compose up --build`.
3. Open `http://localhost:8000/`.

## CLI maintenance

- `python -m app.cli status`
- `python -m app.cli list-tools`
- `python -m app.cli conflicts`

## Included features

- FastAPI chat API and local test UI
- SQLite-backed chat and tool-call history
- File-backed knowledge tools from `app/tools-knowleage`
- OpenAI Agents SDK sessions, tools, and guardrails

## Useful endpoints

- `GET /health`
- `POST /chat/sessions`
- `POST /chat/sessions/{session_id}/messages`
- `GET /chat/sessions/{session_id}`
- `POST /admin/reload-sources` returns disabled status because online ingestion is off
- `GET /admin/sources/status`
- `GET /admin/conflicts`
