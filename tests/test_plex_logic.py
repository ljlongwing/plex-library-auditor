import pytest
import plex_logic
import sqlite3
import json
from datetime import datetime
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Existing tests
# ---------------------------------------------------------------------------

def test_init_db(mock_db):
    conn = sqlite3.connect(mock_db)
    c = conn.cursor()
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='library_cache'")
    assert c.fetchone() is not None
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='series_cache'")
    assert c.fetchone() is not None
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='settings'")
    assert c.fetchone() is not None
    conn.close()

def test_save_get_setting(mock_db):
    plex_logic.save_setting("test_key", "test_value")
    assert plex_logic.get_setting("test_key") == "test_value"

def test_get_series_audit_data_empty(mock_db):
    results = plex_logic.get_series_audit_data()
    assert results == []

def test_get_series_audit_data_with_matching(mock_db):
    conn = sqlite3.connect(mock_db)
    c = conn.cursor()
    c.execute("""
        INSERT INTO library_cache (tmdb_id, title, rating_key, server_id, type, library)
        VALUES (?, ?, ?, ?, ?, ?)
    """, ("123", "The Matrix", "rk1", "sid1", "movie", "Movies"))

    parts = json.dumps([{"id": 123, "title": "The Matrix"}, {"id": 456, "title": "The Matrix Reloaded"}])
    c.execute("""
        INSERT INTO series_cache (collection_id, name, parts, status, last_updated)
        VALUES (?, ?, ?, ?, ?)
    """, ("movie_999", "The Matrix Collection", parts, "Released", datetime.now().isoformat()))
    conn.commit()
    conn.close()

    results = plex_logic.get_series_audit_data()
    assert len(results) == 1
    assert results[0]['name'] == "The Matrix Collection"
    assert results[0]['owned_count'] == 1
    assert results[0]['parts'][0]['is_owned'] is True
    assert results[0]['parts'][1]['is_owned'] is False


# ---------------------------------------------------------------------------
# check_automation_status
# ---------------------------------------------------------------------------

def test_check_automation_status_not_tracked():
    assert plex_logic.check_automation_status("999", "radarr", []) == "Not Tracked"
    assert plex_logic.check_automation_status("999", "sonarr", []) == "Not Tracked"

def test_check_automation_status_radarr_downloaded():
    items = [{"tmdbId": 123, "hasFile": True, "monitored": True}]
    assert plex_logic.check_automation_status("123", "radarr", items) == "Downloaded"

def test_check_automation_status_radarr_monitored():
    items = [{"tmdbId": 123, "hasFile": False, "monitored": True}]
    assert plex_logic.check_automation_status("123", "radarr", items) == "Monitored"

def test_check_automation_status_radarr_tracked_not_monitored():
    items = [{"tmdbId": 123, "hasFile": False, "monitored": False}]
    assert plex_logic.check_automation_status("123", "radarr", items) == "Tracked (Not Monitored)"

def test_check_automation_status_radarr_miss():
    items = [{"tmdbId": 456, "hasFile": True, "monitored": True}]
    assert plex_logic.check_automation_status("123", "radarr", items) == "Not Tracked"

def test_check_automation_status_sonarr_monitored():
    items = [{"tvdbId": 789, "hasFile": False, "monitored": True}]
    assert plex_logic.check_automation_status("789", "sonarr", items) == "Monitored"

def test_check_automation_status_sonarr_percent_complete():
    items = [{"tvdbId": 789, "hasFile": False, "monitored": False,
              "statistics": {"percentOfEpisodes": 100}}]
    assert plex_logic.check_automation_status("789", "sonarr", items) == "Downloaded"


# ---------------------------------------------------------------------------
# add_to_automation
# ---------------------------------------------------------------------------

def test_add_to_automation_not_configured(mock_db, mocker):
    mocker.patch("plex_logic.get_setting", return_value=None)
    success, msg = plex_logic.add_to_automation("123", "radarr", "1", "/movies")
    assert not success
    assert "Not Configured" in msg

def test_add_to_automation_radarr_success(mock_db, mocker):
    plex_logic.save_setting("radarr_url", "http://radarr:7878")
    plex_logic.save_setting("radarr_api_key", "testkey")

    mock_lookup = MagicMock()
    mock_lookup.status_code = 200
    mock_lookup.json.return_value = {"title": "Test Movie", "titleSlug": "test-movie"}

    mock_add = MagicMock()
    mock_add.status_code = 201

    mocker.patch("requests.get", return_value=mock_lookup)
    mocker.patch("requests.post", return_value=mock_add)

    success, msg = plex_logic.add_to_automation("123", "radarr", "1", "/movies", title="Test Movie")
    assert success
    assert "Radarr" in msg

def test_add_to_automation_radarr_lookup_failure(mock_db, mocker):
    plex_logic.save_setting("radarr_url", "http://radarr:7878")
    plex_logic.save_setting("radarr_api_key", "testkey")

    mock_lookup = MagicMock()
    mock_lookup.status_code = 404
    mocker.patch("requests.get", return_value=mock_lookup)

    success, msg = plex_logic.add_to_automation("999", "radarr", "1", "/movies")
    assert not success
    assert "TMDB" in msg

def test_add_to_automation_sonarr_no_tvdb_id(mock_db):
    plex_logic.save_setting("sonarr_url", "http://sonarr:8989")
    plex_logic.save_setting("sonarr_api_key", "testkey")

    success, msg = plex_logic.add_to_automation("", "sonarr", "1", "/tv")
    assert not success
    assert "TVDB" in msg

def test_add_to_automation_sonarr_success(mock_db, mocker):
    plex_logic.save_setting("sonarr_url", "http://sonarr:8989")
    plex_logic.save_setting("sonarr_api_key", "testkey")

    mock_lookup = MagicMock()
    mock_lookup.status_code = 200
    mock_lookup.json.return_value = [{"title": "Test Show", "titleSlug": "test-show"}]

    mock_add = MagicMock()
    mock_add.status_code = 201

    mocker.patch("requests.get", return_value=mock_lookup)
    mocker.patch("requests.post", return_value=mock_add)

    success, msg = plex_logic.add_to_automation("456", "sonarr", "1", "/tv", title="Test Show")
    assert success
    assert "Sonarr" in msg


# ---------------------------------------------------------------------------
# delete_item
# ---------------------------------------------------------------------------

def test_delete_item_removes_from_cache(mock_db, mocker):
    conn = sqlite3.connect(mock_db)
    conn.execute("""INSERT INTO library_cache
        (guid, rating_key, title, type, library, tmdb_id, tvdb_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)""",
        ("g1", "12345", "Test Movie", "movie", "Movies", "603", None))
    conn.commit()
    conn.close()

    mock_server = MagicMock()
    mock_server.allowMediaDeletion = True
    mock_item = MagicMock()
    mock_item.section.return_value = MagicMock()
    mock_server.library.fetchItem.return_value = mock_item
    mocker.patch("plex_logic.PlexServer", return_value=mock_server)
    mocker.patch("plex_logic.unmonitor_in_automation")

    plex_logic._recently_deleted_keys.discard("12345")
    plex_logic.delete_item("http://plex:32400", "token", "12345")

    conn = sqlite3.connect(mock_db)
    row = conn.execute("SELECT * FROM library_cache WHERE rating_key = '12345'").fetchone()
    conn.close()
    assert row is None
    assert "12345" in plex_logic._recently_deleted_keys
    plex_logic._recently_deleted_keys.discard("12345")

def test_delete_item_raises_when_deletion_disabled(mock_db, mocker):
    mock_server = MagicMock()
    mock_server.allowMediaDeletion = False
    mocker.patch("plex_logic.PlexServer", return_value=mock_server)

    with pytest.raises(Exception, match="Allow media deletion"):
        plex_logic.delete_item("http://plex:32400", "token", "rk1")


# ---------------------------------------------------------------------------
# enrich_series_data
# ---------------------------------------------------------------------------

def test_enrich_series_data_movie_collection(mock_db, mocker):
    conn = sqlite3.connect(mock_db)
    conn.execute("""INSERT INTO library_cache
        (guid, rating_key, title, type, library, tmdb_id)
        VALUES (?, ?, ?, ?, ?, ?)""",
        ("g1", "rk1", "The Matrix", "movie", "Movies", "603"))
    conn.commit()
    conn.close()

    movie_resp = MagicMock(status_code=200)
    movie_resp.json.return_value = {
        "belongs_to_collection": {"id": 2344, "name": "The Matrix Collection"}
    }
    collection_resp = MagicMock(status_code=200)
    collection_resp.json.return_value = {
        "parts": [{"id": 603, "title": "The Matrix", "release_date": "1999-03-31", "overview": ""}]
    }
    part_resp = MagicMock(status_code=200)
    part_resp.json.return_value = {"status": "Released"}

    mocker.patch("plex_logic._tmdb_limiter")  # disable rate limiting in tests
    mocker.patch("requests.get", side_effect=[movie_resp, collection_resp, part_resp])

    plex_logic.enrich_series_data("fake_api_key")

    conn = sqlite3.connect(mock_db)
    series = conn.execute(
        "SELECT * FROM series_cache WHERE collection_id = 'movie_2344'"
    ).fetchone()
    conn.close()
    assert series is not None
    assert series[1] == "The Matrix Collection"

def test_enrich_series_data_no_collection(mock_db, mocker):
    conn = sqlite3.connect(mock_db)
    conn.execute("""INSERT INTO library_cache
        (guid, rating_key, title, type, library, tmdb_id)
        VALUES (?, ?, ?, ?, ?, ?)""",
        ("g1", "rk1", "Some Standalone Movie", "movie", "Movies", "999"))
    conn.commit()
    conn.close()

    resp = MagicMock(status_code=200)
    resp.json.return_value = {"belongs_to_collection": None}

    mocker.patch("plex_logic._tmdb_limiter")
    mocker.patch("requests.get", return_value=resp)

    plex_logic.enrich_series_data("fake_api_key")

    conn = sqlite3.connect(mock_db)
    count = conn.execute("SELECT COUNT(*) FROM series_cache").fetchone()[0]
    conn.close()
    assert count == 0

def test_enrich_series_data_tv(mock_db, mocker):
    conn = sqlite3.connect(mock_db)
    conn.execute("""INSERT INTO library_cache
        (guid, rating_key, title, type, library, tmdb_id)
        VALUES (?, ?, ?, ?, ?, ?)""",
        ("g1", "rk1", "Breaking Bad", "show", "TV Shows", "1396"))
    conn.commit()
    conn.close()

    tv_resp = MagicMock(status_code=200)
    tv_resp.json.return_value = {"status": "Ended"}
    ext_resp = MagicMock(status_code=200)
    ext_resp.json.return_value = {"tvdb_id": 81189}

    mocker.patch("plex_logic._tmdb_limiter")
    mocker.patch("requests.get", side_effect=[tv_resp, ext_resp])

    plex_logic.enrich_series_data("fake_api_key")

    conn = sqlite3.connect(mock_db)
    series = conn.execute(
        "SELECT * FROM series_cache WHERE collection_id = 'tv_1396'"
    ).fetchone()
    conn.close()
    assert series is not None
    assert series[3] == "Ended"


# ---------------------------------------------------------------------------
# get_series_audit_data — corrupted parts JSON
# ---------------------------------------------------------------------------

def test_get_series_audit_data_bad_json(mock_db):
    conn = sqlite3.connect(mock_db)
    conn.execute("""INSERT INTO series_cache
        (collection_id, name, parts, status, last_updated)
        VALUES (?, ?, ?, ?, ?)""",
        ("movie_1", "Broken Collection", "NOT VALID JSON", "Released", datetime.now().isoformat()))
    conn.commit()
    conn.close()

    # Should not raise; bad JSON results in the series being skipped (no owned items → deleted)
    results = plex_logic.get_series_audit_data()
    assert isinstance(results, list)
