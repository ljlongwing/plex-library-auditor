# Plex Library Auditor

A Streamlit web app for auditing your Plex library — surface unwatched content, identify incomplete series, and clean up stale media with one click.

## Features

- **OAuth login** — authenticate via plex.tv (supports Google, Apple, and Facebook sign-in)
- **Library Audit** — browse all movies and shows with posters, added dates, and last-watched timestamps; filter by staleness (unwatched, > 3 months, > 6 months, etc.)
- **Series Auditor** — identifies incomplete collections and series; shows which parts you own vs. are missing; integrates with Radarr/Sonarr to add missing content in one click
- **Smart filtering** — filter by content type, library, watch status, and file size
- **Delete from Plex** — remove items directly from the UI; automatically unmonitors them in Radarr/Sonarr if configured
- **Local SQLite cache** — metadata is cached locally for fast browsing without hitting Plex on every page load
- **TMDB enrichment** — pulls collection and series data from TMDB so the Series Auditor knows what belongs together

---

## Quick Start — Docker (recommended)

```bash
git clone https://github.com/ljlongwing/plex-library-auditor.git
cd plex-library-auditor
docker compose up -d
```

Then open **http://localhost:8501** and use the **Settings** tab to configure your Plex connection.

### Persistent data

The compose file mounts `./data` as a Docker volume. Your SQLite cache and settings survive container rebuilds automatically.

### Pre-provisioning with environment variables

You can supply settings via `docker-compose.yml` instead of the UI. Copy the example env file and fill it in:

```bash
cp .env.example .env
# edit .env with your values
```

Then add an `env_file` entry to `docker-compose.yml`:

```yaml
services:
  plexwatched:
    env_file: .env
    ...
```

Or set the variables directly in the `environment:` block — the commented-out keys in `docker-compose.yml` show every supported option.

---

## Quick Start — Portainer

1. In Portainer, go to **Stacks → Add stack**
2. Choose **Repository** as the build method
3. Set the repository URL to:
   ```
   https://github.com/ljlongwing/plex-library-auditor
   ```
4. Leave the compose path as `docker-compose.yml`
5. Under **Environment variables**, add any settings you want to pre-provision (see the [Configuration](#configuration) table below) — or leave them blank and configure everything through the Settings tab after first launch
6. Click **Deploy the stack**

The app will be available on port **8501** of your Docker host. Data is persisted in a `data/` volume relative to the stack.

---

## Quick Start — Local / Manual

### Requirements

- Python 3.10+
- A Plex Media Server and a plex.tv account

### Setup

```bash
git clone https://github.com/ljlongwing/plex-library-auditor.git
cd plex-library-auditor

# use the launch script (handles venv + .env creation automatically)
./launch.sh
```

Or manually:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # optional — you can configure everything in the UI
streamlit run app.py
```

Open **http://localhost:8501**.

---

## Configuration

All settings can be entered through the **Settings** tab in the UI and are persisted to the database. You can also supply them as environment variables (useful for Docker or headless restarts).

| Variable | Required | Description |
|---|---|---|
| `PLEX_TOKEN` | Yes* | Your Plex authentication token |
| `PLEX_URL` | Yes* | URL to your Plex server, e.g. `http://192.168.1.10:32400` |
| `TMDB_API_KEY` | Recommended | [TMDB API key](https://www.themoviedb.org/settings/api) — enables series/collection enrichment |
| `RADARR_URL` | Optional | URL to your Radarr instance |
| `RADARR_API_KEY` | Optional | Radarr API key |
| `RADARR_PROFILE` | Optional | Radarr quality profile ID |
| `RADARR_FOLDER` | Optional | Root folder path for Radarr |
| `SONARR_URL` | Optional | URL to your Sonarr instance |
| `SONARR_API_KEY` | Optional | Sonarr API key |
| `SONARR_PROFILE` | Optional | Sonarr quality profile ID |
| `SONARR_FOLDER` | Optional | Root folder path for Sonarr |
| `CACHE_DB_PATH` | Optional | Path to the SQLite database (default: `cache.sqlite` in project root) |

*\*Can be obtained via the OAuth login flow in the UI instead of setting manually.*

---

## Development

```bash
# Install dev dependencies (includes pytest)
pip install -r requirements-dev.txt

# Run all tests
pytest

# Run a specific test file
pytest tests/test_plex_logic.py
pytest tests/test_app.py
```

### Project structure

```
app.py            — Streamlit UI (three tabs: Library Audit, Series Auditor, Settings)
plex_logic.py     — All backend logic: Plex OAuth, SQLite, TMDB/Radarr/Sonarr, deletion
tests/            — pytest test suite
requirements.txt      — runtime dependencies
requirements-dev.txt  — adds pytest for local development
Dockerfile
docker-compose.yml
```

---

## License

MIT
