import pytest
from streamlit.testing.v1 import AppTest
import plex_logic
from unittest.mock import MagicMock
import pandas as pd

def test_app_smoke(mocker):
    mocker.patch("plex_logic.get_setting", return_value=None)

    at = AppTest.from_file("app.py")
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
    
    at = AppTest.from_file("app.py")
    at.run()
    
    # Should show the navigation (radio)
    assert at.radio[0].value == "Library Audit"
    
    # Should show the data message
    assert "Found 1 items" in at.subheader[0].value
