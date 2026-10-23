# Contributing

Thanks for helping out! Bug reports, ideas and pull requests are all welcome.

## Reporting a bug

Open an issue with what you did, what you expected, what happened, and your setup (Docker or local,
Plex/Radarr/Sonarr versions if relevant). Please remove tokens, API keys and server addresses from
logs and screenshots.

## Pull requests

1. Fork the repo and create a branch from `master`.
2. Set up and run the tests:
   ```bash
   python -m venv venv && source venv/bin/activate
   pip install -r requirements-dev.txt
   pytest
   ```
3. Keep the split: backend logic in `plex_logic.py` (no Streamlit imports), UI in `app.py`.
4. Add or update tests for what you change. The `test` check must pass before a PR can be merged.
5. Keep PRs focused: one fix or feature per PR is much easier to review.

Every PR is reviewed before merging. For larger changes, opening an issue first to talk through
the approach saves everyone time.

## Security

Please don't open a public issue for security problems. Use GitHub's
[private vulnerability reporting](../../security/advisories/new) instead.
