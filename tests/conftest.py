import pytest
import sqlite3
import os
from unittest.mock import MagicMock

@pytest.fixture
def mock_db(tmp_path):
    db_path = tmp_path / "test_cache.sqlite"
    # Patch DB_PATH in plex_logic
    import plex_logic
    original_path = plex_logic.DB_PATH
    plex_logic.DB_PATH = str(db_path)
    
    plex_logic.init_db()
    yield str(db_path)
    
    plex_logic.DB_PATH = original_path

@pytest.fixture
def mock_plex_server(mocker):
    mock_server = MagicMock()
    mock_server.machineIdentifier = "test_machine_id"
    mocker.patch("plex_logic.PlexServer", return_value=mock_server)
    return mock_server
