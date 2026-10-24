import pytest
import pandas as pd
import time
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
    assert "Radarr lookup failed" in msg

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
# delete_items
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
    mocker.patch("plex_logic._unmonitor_in_automation", return_value=[])

    plex_logic._recently_deleted_keys.discard("12345")
    result = plex_logic.delete_items("http://plex:32400", "token", [{"rk": "12345", "title": "Test Movie", "type": "movie"}])
    assert result == {"deleted": 1, "errors": []}

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
        plex_logic.delete_items("http://plex:32400", "token", [{"rk": "1", "title": "x"}])


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


def test_series_scan_saves_in_batches_and_keeps_work_on_failure(mock_db, mocker):
    """An interrupted scan must keep the results it already finished."""
    import sqlite3
    conn = sqlite3.connect(mock_db)
    for i in range(5):
        conn.execute("INSERT INTO library_cache (guid, rating_key, title, type, tmdb_id) VALUES (?, ?, ?, 'movie', ?)",
                     (f"g{i}", f"rk{i}", f"Movie {i}", str(100 + i)))
    conn.commit()
    conn.close()

    def fake_fetch(row, key):
        # One lookup fails, and only after the others have finished (lookups run in parallel threads)
        if row["tmdb_id"] == "104":
            time.sleep(0.5)
            raise RuntimeError("TMDB went away")
        return {"type": "movie", "tmdb_id": row["tmdb_id"], "collection_id": f"movie_{row['tmdb_id']}",
                "name": row["title"], "parts": "[]", "status": "Released"}
    mocker.patch("plex_logic.fetch_tmdb_movie_collection", side_effect=fake_fetch)
    mocker.patch("plex_logic.SCAN_SAVE_EVERY", 2)

    with pytest.raises(RuntimeError):
        plex_logic.enrich_series_data("key")

    conn = sqlite3.connect(mock_db)
    saved = conn.execute("SELECT COUNT(*) FROM series_cache").fetchone()[0]
    conn.close()
    assert saved == 4   # both batches finished before the failure survive


def test_background_series_scan_reports_completion(mock_db, mocker):
    import time as _time
    mocker.patch("plex_logic.enrich_series_data", side_effect=lambda key, progress_callback=None: progress_callback("x", 1.0))
    plex_logic.clear_job_done("series_scan")
    assert plex_logic.start_job("series_scan", plex_logic.enrich_series_data, "key") is True
    for _ in range(50):
        if plex_logic.get_job_state("series_scan")["done"]:
            break
        _time.sleep(0.05)
    state = plex_logic.get_job_state("series_scan")
    assert state["done"] and not state["running"] and state["error"] is None
    plex_logic.clear_job_done("series_scan")


# --- data correctness ---

def _rows(n, start=1):
    return pd.DataFrame([{
        'rating_key': start + i, 'guid': 'plex://movie/same' if i < 2 else f'plex://movie/{i}',
        'title': f'Movie {i}', 'type': 'movie', 'library': 'Movies', 'tmdb_id': str(500 + i),
    } for i in range(n)])


def test_replace_library_cache_keeps_schema_and_text_keys(mock_db):
    # Two rows share a guid (same movie in two libraries): must not break the refresh
    plex_logic.replace_library_cache(_rows(3))
    plex_logic.replace_library_cache(_rows(2, start=10))
    conn = sqlite3.connect(mock_db)
    keys = conn.execute("SELECT rating_key, typeof(rating_key) FROM library_cache ORDER BY rating_key").fetchall()
    pk = [r[1] for r in conn.execute("PRAGMA table_info(library_cache)") if r[5]]
    conn.close()
    assert keys == [("10", "text"), ("11", "text")]   # old rows replaced, keys stored as text
    assert pk == ["rating_key"]                        # table kept its primary key


def test_replace_library_cache_refuses_empty_and_keeps_old_rows(mock_db):
    plex_logic.replace_library_cache(_rows(3))
    with pytest.raises(RuntimeError):
        plex_logic.replace_library_cache(pd.DataFrame())
    conn = sqlite3.connect(mock_db)
    assert conn.execute("SELECT COUNT(*) FROM library_cache").fetchone()[0] == 3
    conn.close()


def test_refresh_with_no_plex_libraries_keeps_cache(mock_db, mocker):
    plex_logic.replace_library_cache(_rows(3))
    server = MagicMock()
    server.history.return_value = []
    server.library.sections.return_value = []
    mocker.patch("plex_logic.PlexServer", return_value=server)
    mocker.patch("plex_logic.get_setting", return_value=None)
    with pytest.raises(RuntimeError):
        plex_logic.fetch_and_cache_data("http://plex:32400", "token")
    conn = sqlite3.connect(mock_db)
    assert conn.execute("SELECT COUNT(*) FROM library_cache").fetchone()[0] == 3
    conn.close()


def test_init_db_repairs_table_made_by_old_refresh(tmp_path, monkeypatch):
    db = tmp_path / "old.sqlite"
    conn = sqlite3.connect(db)
    # What older versions left behind: pandas-made table, no primary key, integer keys
    _rows(2).to_sql("library_cache", conn, index=False)
    conn.close()
    monkeypatch.setattr(plex_logic, "DB_PATH", str(db))
    plex_logic.init_db()
    conn = sqlite3.connect(db)
    keys = conn.execute("SELECT rating_key, typeof(rating_key) FROM library_cache ORDER BY rating_key").fetchall()
    pk = [r[1] for r in conn.execute("PRAGMA table_info(library_cache)") if r[5]]
    conn.close()
    assert keys == [("1", "text"), ("2", "text")]
    assert pk == ["rating_key"]


def test_watched_by_user_matches_numeric_keys_and_shows(mocker):
    history = {"response": {"data": {"data": [
        {"rating_key": 9001, "grandparent_rating_key": 300},   # an episode of show 300
        {"rating_key": 101, "grandparent_rating_key": ""},     # a movie
    ]}}}
    mocker.patch("plex_logic.requests.get", return_value=MagicMock(status_code=200, json=lambda: history))
    keys = plex_logic.get_tautulli_watched_keys("7", url="http://tautulli", api_key="k")
    library = pd.DataFrame({"rating_key": [101, 300, 555], "title": ["Movie", "Show", "Unwatched"]})
    assert set(plex_logic.filter_by_watched_keys(library, keys, watched=True)["title"]) == {"Movie", "Show"}
    assert set(plex_logic.filter_by_watched_keys(library, keys, watched=False)["title"]) == {"Unwatched"}


def test_logout_beats_env_token(mock_db, monkeypatch):
    monkeypatch.setenv("PLEX_TOKEN", "from-env")
    assert plex_logic.get_setting("plex_token") == "from-env"   # nothing saved: env is the fallback
    plex_logic.save_setting("plex_token", None)                  # Logout
    assert plex_logic.get_setting("plex_token") is None


# --- background jobs: bulk delete and upgrades ---

def _cache_rows(conn, rows):
    for rk, title, type_, tmdb, tvdb in rows:
        conn.execute("INSERT INTO library_cache (rating_key, guid, title, type, library, tmdb_id, tvdb_id) VALUES (?, ?, ?, ?, 'Lib', ?, ?)",
                     (rk, f"g{rk}", title, type_, tmdb, tvdb))
    conn.commit()


def test_bulk_delete_one_connection_trash_once_logs_each_and_continues_past_failures(mock_db, mocker):
    conn = sqlite3.connect(mock_db)
    _cache_rows(conn, [("1", "A", "movie", "11", None), ("2", "B", "movie", "12", None),
                       ("3", "C", "show", "13", "903"), ("4", "Gone", "movie", "14", None)])
    conn.close()
    server = MagicMock(allowMediaDeletion=True)
    section = MagicMock(key="1", title="Movies")
    def fetch(rk):
        if rk == 4:
            raise Exception("404 not found")
        return MagicMock(**{"section.return_value": section})
    server.library.fetchItem.side_effect = fetch
    plex_server = mocker.patch("plex_logic.PlexServer", return_value=server)
    unmonitor = mocker.patch("plex_logic._unmonitor_in_automation", return_value=[])
    items = [{"rk": rk, "title": t, "type": ty, "size_bytes": 1} for rk, t, ty in
             [("1", "A", "movie"), ("2", "B", "movie"), ("3", "C", "show"), ("4", "Gone", "movie")]]

    result = plex_logic.delete_items("http://plex", "tok", items)

    assert result["deleted"] == 3 and len(result["errors"]) == 1 and "Gone" in result["errors"][0]
    assert plex_server.call_count == 1                    # one Plex connection for the whole batch
    assert section.emptyTrash.call_count == 1             # trash emptied once per library, not per item
    conn = sqlite3.connect(mock_db)
    logged = sorted(r[0] for r in conn.execute("SELECT title FROM deletion_log"))
    left = [r[0] for r in conn.execute("SELECT rating_key FROM library_cache")]
    conn.close()
    assert logged == ["A", "B", "C"] and left == ["4"]    # audit log matches exactly what Plex removed
    # movies -> Radarr by TMDB id, shows -> Sonarr by TVDB id (a show's TMDB id must not reach Radarr)
    unmonitor.assert_any_call("radarr", ["11", "12"])
    unmonitor.assert_any_call("sonarr", ["903"])
    for _ in ("1", "2", "3"):
        plex_logic._recently_deleted_keys.discard(_)


def test_unmonitor_failure_is_reported_not_swallowed(mock_db, mocker):
    mocker.patch("plex_logic.get_setting", side_effect=lambda k: {"radarr_url": "http://radarr/", "radarr_api_key": "k"}.get(k))
    mocker.patch("plex_logic.requests.get", side_effect=plex_logic.requests.exceptions.Timeout("timed out"))
    errors = plex_logic._unmonitor_in_automation("radarr", ["11"])
    assert errors and "Radarr" in errors[0]


def test_unmonitor_fetches_list_once_and_unmonitors_matches(mock_db, mocker):
    mocker.patch("plex_logic.get_setting", side_effect=lambda k: {"radarr_url": "http://radarr/", "radarr_api_key": "k"}.get(k))
    movies = [{"id": 1, "tmdbId": 11, "monitored": True, "title": "A"}, {"id": 2, "tmdbId": 12, "monitored": True, "title": "B"},
              {"id": 3, "tmdbId": 99, "monitored": True, "title": "Keep"}]
    get = mocker.patch("plex_logic.requests.get", return_value=MagicMock(json=lambda: movies))
    put = mocker.patch("plex_logic.requests.put", return_value=MagicMock())
    assert plex_logic._unmonitor_in_automation("radarr", ["11", "12"]) == []
    assert get.call_count == 1 and get.call_args[0][0] == "http://radarr/api/v3/movie"   # trailing slash trimmed
    assert sorted(c[0][0] for c in put.call_args_list) == ["http://radarr/api/v3/movie/1", "http://radarr/api/v3/movie/2"]
    assert all(c[1]["json"]["monitored"] is False for c in put.call_args_list)


def test_request_upgrades_reports_every_failure(mock_db, mocker):
    mocker.patch("plex_logic.get_automation_items", return_value=[{"tmdbId": 11, "id": 5, "monitored": True}])
    mocker.patch("plex_logic.trigger_radarr_search", return_value=(True, "ok"))
    mocker.patch("plex_logic.get_setting", return_value=None)   # no profile/folder: untracked items can't be added
    result = plex_logic.request_upgrades(
        [{"title": "Tracked", "tmdb_id": 11, "rating_key": "1"}, {"title": "Untracked", "tmdb_id": 22, "rating_key": "2"}], [])
    assert result["ok"] == 1 and len(result["errors"]) == 1 and "Untracked" in result["errors"][0]
