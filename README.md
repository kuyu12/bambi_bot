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

## WordPress knowledge sync

Export a full raw WordPress snapshot and mark only new/changed records compared with the latest previous snapshot:

```powershell
python scripts\export_wordpress_raw.py --sleep 2.5
```

The export writes `manifest.json`, `index.jsonl`, and `changed_files.json` under `data/website_raw/<timestamp>/`. `changed_files.json` contains only pages/posts with `change_status` of `new` or `changed`.

Build knowledge tools only from the new/changed WordPress records:

```powershell
python scripts\build_knowledge_tools.py --only-changed
```

To compare against a specific older snapshot instead of the latest sibling export:

```powershell
python scripts\export_wordpress_raw.py --previous-dir data\website_raw\20260618T214803Z --sleep 2.5
```

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
