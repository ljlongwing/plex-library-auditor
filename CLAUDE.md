# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install runtime dependencies
pip install -r requirements.txt

# Install dev dependencies (includes pytest)
pip install -r requirements-dev.txt

# Run the app
streamlit run app.py
# or via launch script (handles venv setup and .env creation):
./launch.sh

# Run all tests
pytest

# Run a single test file
pytest tests/test_plex_logic.py
pytest tests/test_app.py
```

## Architecture

The app is split into two main modules:

- **`plex_logic.py`** — All backend logic: Plex OAuth, SQLite caching, TMDB/Radarr/Sonarr API calls, deletion, and data transformation. No Streamlit imports.
- **`app.py`** — Streamlit UI layer only. Three tabs: Library Audit, Series Auditor, Settings. Calls into `plex_logic.py` for all data operations.

State is managed via `st.session_state` (tokens, refresh status, pending deletions) and `st.query_params` (tab navigation and filter state persistence in the URL).

## Database

SQLite at `cache.sqlite` (path configurable via settings). Three tables:
- **`library_cache`** — One row per Plex item. Key columns: `guid` (PK), `rating_key`, `type`, `library`, `last_watched_at`, `size_bytes`, `tmdb_id`, `tmdb_collection_id`, `tvdb_id`.
- **`series_cache`** — One row per collection/series. `parts` is JSON-serialized. `collection_id` uses prefix convention: `movie_{tmdb_collection_id}` for movies, `tv_{tmdb_id}` for shows.
- **`settings`** — Key/value store for all user configuration. Settings are also written to `.env` so the app can reconnect headlessly on restart.

`init_db()` in `plex_logic.py` handles schema migrations with `ALTER TABLE` guards.

## Data Flow

**Refresh**: Fetch Plex library sections → process items (shows use `ThreadPoolExecutor` with `max_workers=10`) → extract metadata including collections and file sizes → merge with global play history from `account.history()` → bulk upsert into `library_cache` via pandas.

**Series enrichment**: For each item without TMDB data, call TMDB to get collection IDs (movies) or TVDB IDs (shows) → update `library_cache` → populate `series_cache` with all parts and series status.

**Series audit**: JOIN `library_cache` with `series_cache` → identify owned vs. missing parts per collection → filter/sort for display.

**Delete**: Remove from Plex via `plexapi` → empty server trash → unmonitor in Radarr/Sonarr if configured → remove from `library_cache`.

## External APIs

- **Plex** — via `plexapi` SDK for OAuth, server discovery, library access, and deletion
- **TMDB** — raw `requests` calls for movie collections and TV series status (requires `tmdb_api_key` setting)
- **Radarr / Sonarr** — raw `requests` calls for adding/monitoring missing content; item lists are cached with `@st.cache_data(ttl=300)`

All HTTP requests use 5–10s timeouts.

## Testing

Tests use `pytest` + `pytest-mock` + Streamlit's `AppTest`. Key fixtures in `tests/conftest.py`:
- `mock_db` — creates a temporary SQLite database per test
- `mock_plex_server` — mocked Plex server object

`test_plex_logic.py` covers DB initialization, settings persistence, and series audit logic. `test_app.py` covers smoke tests and basic UI rendering via `AppTest`.
