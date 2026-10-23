import pytest
from streamlit.testing.v1 import AppTest
import plex_logic
from unittest.mock import MagicMock
import pandas as pd
from pathlib import Path

# AppTest resolves relative paths against this test file, not the working directory
APP = str(Path(__file__).resolve().parent.parent / "app.py")

def test_app_smoke(mocker):
    mocker.patch("plex_logic.get_setting", return_value=None)

    at = AppTest.from_file(APP)
    at.run()

    # Check for main title (now in markdown, potentially after CSS injection)
    titles = [m.value for m in at.markdown if "🎬 Plex Library Auditor" in m.value]
    assert len(titles) > 0
    # Unconfigured app defaults to Settings tab
    assert at.radio[0].value == "⚙️ Settings"
    # Navigating to Library Audit shows the welcome prompt
    at.radio[0].set_value("Library Audit").run()
    assert "Please head over to the **⚙️ Settings** tab" in at.warning[0].value

def test_app_with_data(mocker, mock_db):
    # Mock logged in state
    mocker.patch("plex_logic.get_setting", side_effect=lambda k: "mock_token" if k == "plex_token" else ("mock_url" if k == "plex_url" else None))
    
    # Mock load_cached_data to return some test data
    df = pd.DataFrame({
        'guid': ['g1'], 'rating_key': ['rk1'], 'title': ['Test Movie'], 
        'type': ['movie'], 'library': ['Movies'], 'added_at': [pd.Timestamp.now()],
        'latest_added_at': [pd.Timestamp.now()], 'last_watched_at': [pd.NaT],
        'thumb_url': [''], 'viewed_leaf_count': [0], 'leaf_count': [1],
        'server_id': ['sid'], 'size_bytes': [1024**3], 'collections': [''],
        'release_date': [pd.Timestamp.now()], 'resolution': ['1080'],
        'content_rating': ['PG'], 'tmdb_id': ['123'], 'tmdb_collection_id': [None],
        'series_status': ['Released']
    })
    mocker.patch("plex_logic.load_cached_data", return_value=df)
    
    at = AppTest.from_file(APP)
    at.run()
    
    # Should show the navigation (radio)
    assert at.radio[0].value == "Library Audit"
    
    # Should show the data message
    assert "Found 1 items" in at.subheader[0].value


def test_password_gate_blocks_until_unlocked(mocker, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "s3cret")
    mocker.patch("plex_logic.get_setting", side_effect=lambda k: "owner_token" if k == "plex_token" else ("mock_url" if k == "plex_url" else None))

    at = AppTest.from_file(APP)
    at.run()
    # Only the login form renders: no navigation, no library
    assert len(at.radio) == 0
    assert at.text_input[0].label == "Password"

    at.text_input[0].input("wrong")
    at.button[0].click().run()
    assert len(at.radio) == 0
    assert "Wrong password." in at.error[0].value

    at.text_input[0].input("s3cret")
    at.button[0].click().run()
    assert len(at.radio) > 0

def test_no_password_gate_by_default(mocker, monkeypatch):
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    mocker.patch("plex_logic.get_setting", return_value=None)
    at = AppTest.from_file(APP)
    at.run()
    assert len(at.radio) > 0

def test_saved_secrets_are_not_sent_to_the_browser(mocker, monkeypatch):
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    secrets = {"plex_token": "owner_token", "tmdb_api_key": "tmdb_secret", "radarr_api_key": "radarr_secret",
               "sonarr_api_key": "sonarr_secret", "tautulli_api_key": "tautulli_secret"}
    mocker.patch("plex_logic.get_setting", side_effect=lambda k: secrets.get(k))
    mocker.patch("plex_logic.test_tmdb_connection", return_value=(True, "ok"))

    at = AppTest.from_file(APP)
    at.query_params["tab"] = "⚙️ Settings"
    at.run()
    # Manual mode shows the Plex token field
    at.radio[1].set_value("Manual (Direct IP)").run()
    rendered = [w.value for w in at.text_input]
    for secret in secrets.values():
        assert secret not in rendered
    labels = [w.label for w in at.text_input]
    for label in ("Plex Token", "TMDB API Key", "Radarr API Key", "Sonarr API Key", "Tautulli API Key"):
        assert label in labels
