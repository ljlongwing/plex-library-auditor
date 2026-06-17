import sqlite3
import threading
import time
import pandas as pd
from plexapi.myplex import MyPlexAccount, MyPlexPinLogin
from plexapi.server import PlexServer
import os
from dotenv import load_dotenv
from datetime import datetime
import uuid
import requests
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

load_dotenv()

DB_PATH = os.environ.get("CACHE_DB_PATH", "cache.sqlite")
APP_NAME = "PlexLibraryAuditor"

# Rating keys deleted during an active refresh — used to re-apply deletions after
# fetch_and_cache_data overwrites library_cache with a full replace.
_recently_deleted_keys: set = set()

# Background refresh state — written by worker thread, read by Streamlit script.
_refresh_state = {
    "running": False,
    "overall_msg": "",
    "item_msg": "",
    "done": False,
    "error": None,
}

# Locks for cross-thread access to shared module-level state.
_state_lock = threading.Lock()   # guards _refresh_state
_data_lock = threading.Lock()    # guards _recently_deleted_keys


class _RateLimiter:
    """Token-bucket rate limiter safe for concurrent threads."""
    def __init__(self, calls_per_second: float):
        self._interval = 1.0 / calls_per_second
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            now = time.time()
            gap = self._interval - (now - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.time()


# TMDB free tier caps at ~40 req/s; stay at 20/s to leave headroom.
_tmdb_limiter = _RateLimiter(calls_per_second=20)


def test_tmdb_connection(api_key):
    """Test a TMDB API key. Returns (ok: bool, message: str)."""
    try:
        res = requests.get(
            f"https://api.themoviedb.org/3/configuration?api_key={api_key}",
            timeout=10,
        )
        if res.status_code == 200:
            return True, "API key is valid"
        if res.status_code == 401:
            return False, "Invalid API key"
        return False, f"Unexpected response: HTTP {res.status_code}"
    except requests.exceptions.ConnectionError:
        return False, "Could not reach api.themoviedb.org — check network connectivity"
    except requests.exceptions.Timeout:
        return False, "Connection to TMDB timed out"
    except Exception as e:
        return False, str(e)

def _tmdb_get(url: str, timeout: int = 10, max_retries: int = 3):
    """Rate-limited TMDB GET with automatic retry on 429 responses."""
    for attempt in range(max_retries):
        _tmdb_limiter.wait()
        try:
            res = requests.get(url, timeout=timeout)
            if res.status_code == 429:
                retry_after = int(res.headers.get('Retry-After', 10))
                print(f"TMDB rate-limited, backing off {retry_after}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(retry_after)
                continue
            return res
        except requests.RequestException as e:
            if attempt == max_retries - 1:
                print(f"TMDB request failed after {max_retries} attempts: {e}")
                return None
            time.sleep(2 ** attempt)
    return None

def start_background_refresh(plex_url, plex_token):
    """Launch fetch_and_cache_data in a background thread. Returns False if already running."""
    with _state_lock:
        if _refresh_state["running"]:
            return False
        _refresh_state.update({"running": True, "overall_msg": "Starting...", "item_msg": "", "done": False, "error": None})

    def _progress(msg, _pct, is_overall=False):
        with _state_lock:
            if is_overall:
                _refresh_state["overall_msg"] = msg
                _refresh_state["item_msg"] = ""
            else:
                _refresh_state["item_msg"] = msg

    def _run():
        try:
            fetch_and_cache_data(plex_url, plex_token, progress_callback=_progress)
        except Exception as e:
            with _state_lock:
                _refresh_state["error"] = str(e)
        finally:
            with _state_lock:
                _refresh_state["done"] = True
                _refresh_state["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return True

def get_refresh_state():
    with _state_lock:
        return dict(_refresh_state)

def clear_refresh_done():
    with _state_lock:
        _refresh_state.update({"done": False, "error": None, "overall_msg": "", "item_msg": ""})

def get_client_id():
    """Retrieve or create a stable client identifier."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    c.execute("SELECT value FROM settings WHERE key = 'client_id'")
    row = c.fetchone()
    if row:
        client_id = row[0]
    else:
        client_id = str(uuid.uuid4())
        c.execute("INSERT INTO settings (key, value) VALUES ('client_id', ?)", (client_id,))
        conn.commit()
    conn.close()
    return client_id

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Table for metadata
    c.execute('''CREATE TABLE IF NOT EXISTS library_cache
                 (guid TEXT PRIMARY KEY, rating_key TEXT, title TEXT, type TEXT, library TEXT,
                  added_at DATETIME, last_watched_at DATETIME, thumb_url TEXT,
                  viewed_leaf_count INTEGER, leaf_count INTEGER, server_id TEXT,
                  size_bytes INTEGER, latest_added_at DATETIME, collections TEXT,
                  release_date DATETIME, resolution TEXT, content_rating TEXT,
                  tmdb_id TEXT, tmdb_collection_id TEXT, tvdb_id TEXT)''')
    
    # Check for missing columns (Schema updates)
    c.execute("PRAGMA table_info(library_cache)")
    lib_columns = [row[1] for row in c.fetchall()]
    if 'tvdb_id' not in lib_columns:
        c.execute("ALTER TABLE library_cache ADD COLUMN tvdb_id TEXT")

    # Table for series/collections from TMDB
    c.execute('''CREATE TABLE IF NOT EXISTS series_cache
                 (collection_id TEXT PRIMARY KEY, name TEXT, parts TEXT, status TEXT, 
                  is_hidden INTEGER DEFAULT 0, last_updated DATETIME, tvdb_id TEXT)''')
    
    # Check if 'status' column exists in series_cache (for older installs)
    c.execute("PRAGMA table_info(series_cache)")
    columns = [row[1] for row in c.fetchall()]
    if 'status' not in columns:
        c.execute("ALTER TABLE series_cache ADD COLUMN status TEXT")
    if 'is_hidden' not in columns:
        c.execute("ALTER TABLE series_cache ADD COLUMN is_hidden INTEGER DEFAULT 0")
    if 'tvdb_id' not in columns:
        c.execute("ALTER TABLE series_cache ADD COLUMN tvdb_id TEXT")
        
    # Data migration: Ensure collection_ids have prefixes
    # 1. Update library_cache
    c.execute("""
        UPDATE library_cache 
        SET tmdb_collection_id = 'movie_' || tmdb_collection_id 
        WHERE type = 'movie' 
        AND tmdb_collection_id IS NOT NULL 
        AND tmdb_collection_id != ''
        AND tmdb_collection_id NOT LIKE 'movie_%'
    """)
    c.execute("""
        UPDATE library_cache 
        SET tmdb_collection_id = 'tv_' || tmdb_collection_id 
        WHERE type = 'show' 
        AND tmdb_collection_id IS NOT NULL 
        AND tmdb_collection_id != ''
        AND tmdb_collection_id NOT LIKE 'tv_%'
    """)
    
    # 2. Cleanup series_cache duplicates before updating
    # Delete non-prefixed if prefixed exists
    c.execute("""
        DELETE FROM series_cache 
        WHERE collection_id NOT LIKE 'movie_%' AND collection_id NOT LIKE 'tv_%'
        AND (
            'movie_' || collection_id IN (SELECT collection_id FROM series_cache)
            OR 'tv_' || collection_id IN (SELECT collection_id FROM series_cache)
        )
    """)

    # 3. Update remaining series_cache (Movies)
    c.execute("""
        UPDATE series_cache
        SET collection_id = 'movie_' || collection_id
        WHERE collection_id NOT LIKE 'movie_%'
        AND collection_id NOT LIKE 'tv_%'
        AND (tvdb_id IS NULL OR tvdb_id = '')
    """)
    # 4. Update remaining series_cache (TV)
    c.execute("""
        UPDATE series_cache
        SET collection_id = 'tv_' || collection_id
        WHERE collection_id NOT LIKE 'movie_%'
        AND collection_id NOT LIKE 'tv_%'
        AND tvdb_id IS NOT NULL AND tvdb_id != ''
    """)

    # Table for settings
    c.execute('''CREATE TABLE IF NOT EXISTS settings
                 (key TEXT PRIMARY KEY, value TEXT)''')

    # Table for deletion audit log
    c.execute('''CREATE TABLE IF NOT EXISTS deletion_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        deleted_at TEXT,
        title TEXT,
        type TEXT,
        library TEXT,
        size_bytes INTEGER,
        rating_key TEXT
    )''')

    conn.commit()
    conn.close()
    get_client_id()

def get_automation_configs(service_type):
    """Fetches quality profiles and root folders from Radarr or Sonarr."""
    url_key = f"{service_type}_url"
    api_key_name = f"{service_type}_api_key"
    
    url = get_setting(url_key)
    api_key = get_setting(api_key_name)
    
    if not url or not api_key:
        return {"profiles": [], "folders": []}
    
    base = url.rstrip('/')
    try:
        p_res = requests.get(f"{base}/api/v3/qualityprofile", headers={"X-Api-Key": api_key}, timeout=10)
        profiles = [{"id": p["id"], "name": p["name"]} for p in p_res.json()] if p_res.status_code == 200 else []

        f_res = requests.get(f"{base}/api/v3/rootfolder", headers={"X-Api-Key": api_key}, timeout=10)
        folders = [{"path": f["path"], "accessible": f["accessible"]} for f in f_res.json()] if f_res.status_code == 200 else []

        return {"profiles": profiles, "folders": folders, "error": None}
    except Exception as e:
        print(f"Error fetching {service_type} configs: {e}")
        return {"profiles": [], "folders": [], "error": str(e)}

def _automation_url(base_url, service_type):
    return f"{base_url.rstrip('/')}/api/v3/{'movie' if service_type == 'radarr' else 'series'}"

def get_automation_items(service_type, url=None, api_key=None):
    """Fetches all items from Radarr or Sonarr for cached status checking.
    url and api_key override saved settings when provided (e.g. for connection testing).
    """
    if url is None:
        url = get_setting(f"{service_type}_url")
    if api_key is None:
        api_key = get_setting(f"{service_type}_api_key")

    if not url or not api_key:
        return []

    try:
        res = requests.get(_automation_url(url, service_type), headers={"X-Api-Key": api_key}, timeout=10)
        return res.json() if res.status_code == 200 else []
    except Exception as e:
        print(f"Error fetching {service_type} items: {e}")
        return []

def test_automation_connection(service_type, url, api_key):
    """Test connection to Radarr/Sonarr and return (items, error_message)."""
    try:
        res = requests.get(_automation_url(url, service_type), headers={"X-Api-Key": api_key}, timeout=10)
        if res.status_code == 200:
            return res.json(), None
        return [], f"HTTP {res.status_code} — check your API key"
    except requests.exceptions.ConnectionError:
        return [], f"Could not connect to {url} — check the URL and that the service is reachable"
    except requests.exceptions.Timeout:
        return [], f"Connection to {url} timed out"
    except Exception as e:
        return [], str(e)

def check_automation_status(external_id, service_type, all_items):
    """Checks status using a pre-fetched list of items.
    external_id is a TMDB ID for Radarr, or a TVDB ID for Sonarr.
    """
    if not all_items:
        return "Not Tracked"

    if service_type == "radarr":
        match = next((i for i in all_items if str(i.get('tmdbId')) == str(external_id)), None)
    else:
        match = next((i for i in all_items if str(i.get('tvdbId')) == str(external_id)), None)
        
    if match:
        if match.get('hasFile') or match.get('statistics', {}).get('percentOfEpisodes') == 100: 
            return "Downloaded"
        if match.get('monitored'): 
            return "Monitored"
        return "Tracked (Not Monitored)"
    
    return "Not Tracked"

def get_radarr_profile_max_resolution(profile_id):
    """Returns the max resolution (px) the given Radarr quality profile allows, e.g. 1080 or 2160."""
    url = get_setting("radarr_url")
    api_key = get_setting("radarr_api_key")
    if not url or not api_key or not profile_id:
        return 0
    try:
        res = requests.get(f"{url.rstrip('/')}/api/v3/qualityprofile/{profile_id}",
                           headers={"X-Api-Key": api_key}, timeout=10)
        if res.status_code != 200:
            return 0
        profile = res.json()
        max_res = 0
        for item in profile.get("items", []):
            if not item.get("allowed"):
                continue
            sub_items = item.get("items") or []
            if sub_items:
                # Group: items are nested one level deeper, each with a "quality" key
                for sub in sub_items:
                    if sub.get("allowed") is not False:
                        max_res = max(max_res, sub.get("quality", {}).get("resolution", 0))
            else:
                max_res = max(max_res, item.get("quality", {}).get("resolution", 0))
        return max_res
    except Exception:
        return 0

def trigger_radarr_search(tmdb_id, all_radarr_items):
    """Triggers a Radarr upgrade search for a movie already tracked in Radarr."""
    url = get_setting("radarr_url")
    api_key = get_setting("radarr_api_key")
    if not url or not api_key:
        return False, "Radarr not configured"
    match = next((i for i in all_radarr_items if str(i.get('tmdbId')) == str(tmdb_id)), None)
    if not match:
        return False, "Movie not found in Radarr"
    try:
        res = requests.post(
            f"{url.rstrip('/')}/api/v3/command",
            headers={"X-Api-Key": api_key},
            json={"name": "MoviesSearch", "movieIds": [match["id"]]},
            timeout=10
        )
        if res.status_code in [200, 201, 202]:
            return True, "Upgrade search triggered"
        return False, f"Radarr HTTP {res.status_code}: {res.text}"
    except Exception as e:
        return False, str(e)

def get_sonarr_profile_max_resolution(profile_id):
    """Returns the max resolution (px) the given Sonarr quality profile allows, e.g. 1080 or 2160."""
    url = get_setting("sonarr_url")
    api_key = get_setting("sonarr_api_key")
    if not url or not api_key or not profile_id:
        return 0
    try:
        res = requests.get(f"{url.rstrip('/')}/api/v3/qualityprofile/{profile_id}",
                           headers={"X-Api-Key": api_key}, timeout=10)
        if res.status_code != 200:
            return 0
        profile = res.json()
        max_res = 0
        for item in profile.get("items", []):
            if not item.get("allowed"):
                continue
            sub_items = item.get("items") or []
            if sub_items:
                for sub in sub_items:
                    if sub.get("allowed") is not False:
                        max_res = max(max_res, sub.get("quality", {}).get("resolution", 0))
            else:
                max_res = max(max_res, item.get("quality", {}).get("resolution", 0))
        return max_res
    except Exception:
        return 0

def trigger_sonarr_search(tvdb_id, all_sonarr_items):
    """Triggers a Sonarr upgrade search for a series already tracked in Sonarr."""
    url = get_setting("sonarr_url")
    api_key = get_setting("sonarr_api_key")
    if not url or not api_key:
        return False, "Sonarr not configured"
    match = next((i for i in all_sonarr_items if str(i.get('tvdbId')) == str(tvdb_id)), None)
    if not match:
        return False, "Series not found in Sonarr"
    try:
        res = requests.post(
            f"{url.rstrip('/')}/api/v3/command",
            headers={"X-Api-Key": api_key},
            json={"name": "SeriesSearch", "seriesId": match["id"]},
            timeout=10
        )
        if res.status_code in [200, 201, 202]:
            return True, "Upgrade search triggered"
        return False, f"Sonarr HTTP {res.status_code}: {res.text}"
    except Exception as e:
        return False, str(e)

def add_to_automation(external_id, service_type, profile_id, root_folder, title=None):
    """Adds a new movie or show to Radarr/Sonarr.
    external_id is a TMDB ID for Radarr movies, or a TVDB ID for Sonarr shows.
    """
    url = get_setting(f"{service_type}_url")
    api_key = get_setting(f"{service_type}_api_key")

    if not url or not api_key:
        return False, "Not Configured"

    try:
        if service_type == "radarr":
            base = url.rstrip('/')
            lookup_res = requests.get(f"{base}/api/v3/movie/lookup/tmdb?tmdbId={external_id}", headers={"X-Api-Key": api_key}, timeout=10)
            if lookup_res.status_code != 200:
                return False, f"Radarr lookup failed (HTTP {lookup_res.status_code})"

            movie_data = lookup_res.json()
            payload = {
                "title": movie_data["title"],
                "qualityProfileId": int(profile_id),
                "titleSlug": movie_data["titleSlug"],
                "tmdbId": int(external_id),
                "rootFolderPath": root_folder,
                "monitored": True,
                "addOptions": {"searchForMovie": True}
            }
            add_res = requests.post(f"{base}/api/v3/movie", headers={"X-Api-Key": api_key}, json=payload, timeout=10)
            if add_res.status_code in [200, 201]:
                return True, "Added to Radarr"
            return False, f"Radarr HTTP {add_res.status_code}: {add_res.text}"
        else:
            if not external_id:
                return False, "TVDB ID is required for Sonarr"

            base = url.rstrip('/')
            lookup_res = requests.get(f"{base}/api/v3/series/lookup?term=tvdb:{external_id}", headers={"X-Api-Key": api_key}, timeout=10)
            if lookup_res.status_code != 200:
                return False, f"Sonarr lookup failed (HTTP {lookup_res.status_code})"

            series_data = lookup_res.json()[0]
            payload = {
                "title": series_data["title"],
                "qualityProfileId": int(profile_id),
                "titleSlug": series_data["titleSlug"],
                "tvdbId": int(external_id),
                "rootFolderPath": root_folder,
                "monitored": True,
                "addOptions": {"searchForMissingEpisodes": True}
            }
            add_res = requests.post(f"{base}/api/v3/series", headers={"X-Api-Key": api_key}, json=payload, timeout=10)
            if add_res.status_code in [200, 201]:
                return True, "Added to Sonarr"
            return False, f"Sonarr HTTP {add_res.status_code}: {add_res.text}"

    except Exception as e:
        return False, str(e)

def save_setting(key, value):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

def get_setting(key):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    if row and row[0]:
        return row[0]
    return os.environ.get(key.upper())

def start_plex_auth():
    """Starts the OAuth PIN flow. Returns (pin_login, pin_id) tuple.

    run() is called here because newer plexapi versions populate _id only
    after run() fetches the PIN from plex.tv. The browser-open step inside
    run() will silently fail in Docker, which is fine.
    """
    client_id = get_client_id()
    headers = {
        'X-Plex-Product': APP_NAME,
        'X-Plex-Client-Identifier': client_id,
        'X-Plex-Version': '1.0.0'
    }
    pin_login = MyPlexPinLogin(headers=headers, oauth=True)
    try:
        pin_login.run()
    except Exception:
        pass  # browser open fails in Docker; _id and _code are still set
    pin_id = getattr(pin_login, '_id', None) or getattr(pin_login, 'id', None)
    if not pin_id:
        raise RuntimeError(
            f"Could not find PIN ID on MyPlexPinLogin after run(). "
            f"Available attributes: {[a for a in dir(pin_login) if not a.startswith('__')]}"
        )
    return pin_login, pin_id

def check_plex_pin(pin_id):
    """Directly poll plex.tv to check if the OAuth PIN has been authorized.

    plexapi's checkLogin() only reads cached thread state, which is unreliable
    in headless/Docker environments. This makes a fresh request instead.
    """
    client_id = get_client_id()
    headers = {
        'Accept': 'application/json',
        'X-Plex-Product': APP_NAME,
        'X-Plex-Client-Identifier': client_id,
        'X-Plex-Version': '1.0.0',
    }
    resp = requests.get(
        f'https://plex.tv/api/v2/pins/{pin_id}',
        headers=headers,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get('authToken')

def get_plex_account(token):
    return MyPlexAccount(token=token)

def process_show_episodes(item, global_history):
    size_bytes = 0
    viewed_count = 0
    ep_dates = []
    ep_added_dates = []
    resolutions = set()
    
    try:
        episodes = item.episodes()
        for ep in episodes:
            ep._autoReload = False
            is_watched = False
            # Check multiple indicators for watch status
            if ep.isWatched or (ep.viewCount and ep.viewCount > 0) or (ep.lastViewedAt is not None):
                is_watched = True
                if ep.lastViewedAt: ep_dates.append(ep.lastViewedAt)
            
            if str(ep.ratingKey) in global_history:
                is_watched = True
                ep_dates.append(global_history[str(ep.ratingKey)])
            
            if is_watched:
                viewed_count += 1
            
            if ep.addedAt:
                ep_added_dates.append(ep.addedAt)
                
            for media in ep.media:
                if media.videoResolution:
                    resolutions.add(media.videoResolution)
                for part in media.parts:
                    size_bytes += (part.size or 0)
        
        last_watched = max(ep_dates) if ep_dates else None
        latest_added_at = max(ep_added_dates) if ep_added_dates else item.addedAt
        
        resolution = ""
        if resolutions:
            res_priority = {"4k": 4, "1080": 3, "720": 2, "sd": 1}
            sorted_res = sorted(list(resolutions), key=lambda r: res_priority.get(r.lower(), 0), reverse=True)
            resolution = sorted_res[0]
            
        return viewed_count, len(episodes), size_bytes, latest_added_at, resolution, last_watched
    except Exception as e:
        print(f"Error processing episodes for {getattr(item, 'title', 'unknown')}: {e}")
        return getattr(item, 'viewedLeafCount', 0), getattr(item, 'leafCount', 0), 0, item.addedAt, "", item.lastViewedAt

def process_movie_item(item, global_history, collection_map, machine_id, section_title, existing_mappings):
    """Process a single movie item into a cache row dict."""
    rk = str(item.ratingKey)
    last_watched = item.lastViewedAt
    if rk in global_history:
        gh_time = global_history[rk]
        if last_watched is None or gh_time > last_watched:
            last_watched = gh_time

    size_bytes = 0
    resolution = ""
    if item.media:
        resolution = item.media[0].videoResolution
    for media in item.media:
        for part in media.parts:
            size_bytes += (part.size or 0)

    viewed_count = 0
    if (item.isWatched or
            (getattr(item, 'viewCount', 0) or 0) > 0 or
            item.lastViewedAt is not None or
            rk in global_history or
            (getattr(item, 'viewedLeafCount', 0) or 0) > 0):
        viewed_count = 1

    tmdb_id = None
    tvdb_id = None
    for guid_elem in item._data.findall('Guid'):
        guid_id = guid_elem.get('id', '')
        if 'tmdb://' in guid_id:
            tmdb_id = guid_id.split('tmdb://')[-1]
        if 'tvdb://' in guid_id:
            tvdb_id = guid_id.split('tvdb://')[-1]

    return {
        'guid': item.guid,
        'rating_key': item.ratingKey,
        'title': item.title,
        'type': 'movie',
        'library': section_title,
        'added_at': item.addedAt,
        'last_watched_at': last_watched,
        'thumb_url': item.thumbUrl,
        'viewed_leaf_count': viewed_count,
        'leaf_count': 1,
        'server_id': machine_id,
        'size_bytes': size_bytes,
        'latest_added_at': item.addedAt,
        'collections': ", ".join(collection_map.get(rk, [])),
        'release_date': item.originallyAvailableAt,
        'resolution': resolution,
        'content_rating': item.contentRating,
        'tmdb_id': tmdb_id,
        'tmdb_collection_id': existing_mappings.get(tmdb_id),
        'tvdb_id': tvdb_id,
    }


def fetch_and_cache_data(plex_url, plex_token, progress_callback=None):
    plex = PlexServer(plex_url, plex_token)
    machine_id = plex.machineIdentifier
    all_data = []
    
    if progress_callback:
        progress_callback("Fetching global play history...", 0.05, is_overall=True)
    
    global_history = {}
    try:
        # Fetch a very large number of play history items in a single call.
        # This is more reliable than manual pagination on some Plex servers.
        # 100,000 items effectively covers "unlimited" history for almost all users.
        if progress_callback:
            progress_callback("Fetching play history...", 0.05, is_overall=True)
        
        history = plex.history(maxresults=100000)
        for entry in history:
            rk = str(entry.ratingKey)
            if rk not in global_history or (entry.viewedAt and entry.viewedAt > global_history[rk]):
                global_history[rk] = entry.viewedAt
    except Exception as e:
        if progress_callback:
            progress_callback(f"Note: Could not fetch global history ({e})", 0.05, is_overall=True)

    sections = plex.library.sections()
    valid_sections = [s for s in sections if s.type in ['movie', 'show']]
    total_sections = len(valid_sections)
    current_section = 0

    existing_mappings = {}
    try:
        conn_check = sqlite3.connect(DB_PATH)
        m_df = pd.read_sql("SELECT tmdb_id, tmdb_collection_id FROM library_cache WHERE tmdb_collection_id IS NOT NULL", conn_check)
        existing_mappings = dict(zip(m_df['tmdb_id'], m_df['tmdb_collection_id']))
        conn_check.close()
    except Exception as e:
        print(f"Could not load existing TMDB mappings: {e}")

    for section in valid_sections:
        current_section += 1
        overall_pct = (current_section - 1) / total_sections
        sec_prefix = f"Library {current_section}/{total_sections}: {section.title}"
        if progress_callback:
            progress_callback(f"{sec_prefix} — fetching item list...", overall_pct, is_overall=True)

        # Bulk fetch with GUIDs. includeCollections=1 does NOT embed <Collection> child
        # elements in the section listing XML — those only appear in full item metadata.
        # We fetch collection membership separately below via the collections endpoint.
        key = f"/library/sections/{section.key}/all"
        params = {'includeGuids': 1}
        try:
            data = section._server.query(key, params=params)
            items = section.findItems(data)
        except Exception as e:
            print(f"Optimized query failed for section '{section.title}', falling back: {e}")
            items = section.all(includeGuids=1)

        # Prevent plexapi from auto-reloading individual items when attributes are None.
        # Items from section listings are "partial objects"; plexapi's __getattribute__
        # calls reload() for any attribute that returns None/[], hitting /library/metadata/{ratingKey}
        # per item and causing Plex to queue metadata refreshes. We want None to mean None.
        for item in items:
            item._autoReload = False

        total_items = len(items)

        # Build ratingKey → [collection names] map for this section.
        # One call per section (not per item) via the collections endpoint.
        if progress_callback:
            progress_callback(f"{sec_prefix} — building collection map ({total_items} items)...", overall_pct, is_overall=True)
        collection_map = {}
        try:
            colls_data = section._server.query(f"/library/sections/{section.key}/collections")
            for coll_elem in colls_data:
                coll_title = coll_elem.get('title', '')
                coll_rk = coll_elem.get('ratingKey', '')
                if coll_rk and coll_title:
                    children = section._server.query(f"/library/metadata/{coll_rk}/children")
                    for child in children:
                        item_rk = child.get('ratingKey', '')
                        if item_rk:
                            collection_map.setdefault(item_rk, []).append(coll_title)
        except Exception as e:
            print(f"Could not fetch collections for section '{section.title}': {e}")

        if progress_callback:
            progress_callback(f"{sec_prefix} (0/{total_items})", overall_pct, is_overall=True)

        # To speed up TV shows, we parallelize episode fetching
        if section.type == 'show':
            with ThreadPoolExecutor(max_workers=10) as executor:
                future_to_item = {executor.submit(process_show_episodes, item, global_history): item for item in items}

                for idx, future in enumerate(as_completed(future_to_item)):
                    item = future_to_item[future]
                    if progress_callback and idx % 10 == 0:
                        progress_callback(f"{sec_prefix} ({idx}/{total_items})", overall_pct, is_overall=True)
                        progress_callback(f"Processing: {item.title}", idx / total_items if total_items > 0 else 0)
                    
                    try:
                        viewed_count, leaf_count, size_bytes, latest_added_at, resolution, last_watched = future.result()
                    except Exception as e:
                        print(f"Error processing show '{item.title}': {e}")
                        viewed_count, leaf_count, size_bytes, latest_added_at, resolution, last_watched = 0, 0, 0, item.addedAt, "", item.lastViewedAt

                    collections = ", ".join(collection_map.get(str(item.ratingKey), []))
                    
                    tmdb_id = None
                    tvdb_id = None
                    for guid_elem in item._data.findall('Guid'):
                        guid_id = guid_elem.get('id', '')
                        if 'tmdb://' in guid_id:
                            tmdb_id = guid_id.split('tmdb://')[-1]
                        if 'tvdb://' in guid_id:
                            tvdb_id = guid_id.split('tvdb://')[-1]

                    all_data.append({
                        'guid': item.guid,
                        'rating_key': item.ratingKey,
                        'title': item.title,
                        'type': section.type,
                        'library': section.title,
                        'added_at': item.addedAt,
                        'last_watched_at': last_watched,
                        'thumb_url': item.thumbUrl,
                        'viewed_leaf_count': viewed_count,
                        'leaf_count': leaf_count,
                        'server_id': machine_id,
                        'size_bytes': size_bytes,
                        'latest_added_at': latest_added_at,
                        'collections': collections,
                        'release_date': item.originallyAvailableAt,
                        'resolution': resolution,
                        'content_rating': item.contentRating,
                        'tmdb_id': tmdb_id,
                        'tmdb_collection_id': existing_mappings.get(tmdb_id),
                        'tvdb_id': tvdb_id
                    })
        else:
            with ThreadPoolExecutor(max_workers=10) as executor:
                future_to_item = {
                    executor.submit(
                        process_movie_item, item, global_history,
                        collection_map, machine_id, section.title, existing_mappings
                    ): item
                    for item in items
                }
                for idx, future in enumerate(as_completed(future_to_item)):
                    item = future_to_item[future]
                    if progress_callback and idx % 20 == 0:
                        progress_callback(f"{sec_prefix} ({idx}/{total_items})", overall_pct, is_overall=True)
                        progress_callback(f"Processing: {item.title}", idx / total_items if total_items > 0 else 0)
                    try:
                        all_data.append(future.result())
                    except Exception as e:
                        print(f"Error processing movie '{item.title}': {e}")

    if progress_callback:
        progress_callback("Saving to local cache...", 1.0, is_overall=True)
        progress_callback("Done", 1.0)

    df = pd.DataFrame(all_data)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    if not df.empty:
        # Write to staging first — if this fails, old data in library_cache is preserved
        df.to_sql('library_cache_staging', conn, if_exists='replace', index=False)
        # Atomic swap: only drop old data after new data is ready
        c.execute("DROP TABLE IF EXISTS library_cache")
        c.execute("ALTER TABLE library_cache_staging RENAME TO library_cache")
    else:
        c.execute("DELETE FROM library_cache")
    conn.commit()
    conn.close()

    # Ensure schema is current (adds any new columns added in migrations)
    init_db()

    save_setting('last_refreshed_at', datetime.now().isoformat())

    # Re-apply any deletions that happened while the refresh was running.
    with _data_lock:
        if _recently_deleted_keys:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            keys = list(_recently_deleted_keys)
            c.execute(
                f"DELETE FROM library_cache WHERE rating_key IN ({','.join(['?'] * len(keys))})",
                keys,
            )
            conn.commit()
            conn.close()
            _recently_deleted_keys.difference_update(set(keys))

    return df

def load_cached_data():
    conn = sqlite3.connect(DB_PATH)
    try:
        # Join with series_cache to get status (Continuing/Ended)
        query = """
            SELECT lc.*, sc.status as series_status 
            FROM library_cache lc
            LEFT JOIN series_cache sc ON lc.tmdb_collection_id = sc.collection_id
        """
        df = pd.read_sql(query, conn)
        
        if not df.empty:
            df['added_at'] = pd.to_datetime(df['added_at'])
            df['latest_added_at'] = pd.to_datetime(df['latest_added_at'])
            df['last_watched_at'] = pd.to_datetime(df['last_watched_at'])
            df['release_date'] = pd.to_datetime(df['release_date'])
    except Exception as e:
        print(f"Error loading cached data: {e}")
        df = pd.DataFrame()
    finally:
        conn.close()
    return df

def unmonitor_in_automation(external_id, service_type):
    """Unmonitors an item in Radarr or Sonarr to prevent re-downloads after deletion."""
    url = get_setting(f"{service_type}_url")
    api_key = get_setting(f"{service_type}_api_key")
    if not url or not api_key or not external_id:
        return

    try:
        endpoint = "movie" if service_type == "radarr" else "series"
        id_field = "tmdbId" if service_type == "radarr" else "tvdbId"
        
        # 1. Find the item
        res = requests.get(f"{url}/api/v3/{endpoint}", headers={"X-Api-Key": api_key}, timeout=5)
        if res.status_code == 200:
            items = res.json()
            match = next((i for i in items if str(i.get(id_field)) == str(external_id)), None)
            
            if match and match.get('monitored'):
                # 2. Update to unmonitored
                match['monitored'] = False
                put_url = f"{url}/api/v3/{endpoint}/{match['id']}"
                requests.put(put_url, headers={"X-Api-Key": api_key}, json=match, timeout=5)
    except Exception as e:
        print(f"Error unmonitoring {service_type} item: {e}")

def delete_item(plex_url, plex_token, rating_key):
    """Deletes an item from Plex and the local cache."""
    # Fetch external IDs before deleting from cache
    tmdb_id = None
    tvdb_id = None
    try:
        conn_pre = sqlite3.connect(DB_PATH)
        row = conn_pre.execute("SELECT tmdb_id, tvdb_id, type FROM library_cache WHERE rating_key = ?", (str(rating_key),)).fetchone()
        if row:
            tmdb_id, tvdb_id, item_type = row
        conn_pre.close()
    except Exception as e:
        print(f"Could not pre-fetch external IDs for rating_key {rating_key}: {e}")

    # 1. Delete from Plex
    try:
        plex = PlexServer(plex_url, plex_token)
        
        # Check if the server allows deletion
        if not getattr(plex, 'allowMediaDeletion', False):
            raise Exception("Plex Server has 'Allow media deletion' DISABLED. Please enable it in Settings > Server > Library to delete files.")

        item = plex.library.fetchItem(int(rating_key))
        section = item.section()
        item.delete()
        
        # 2. Trigger Empty Trash for the section to prevent ghosts
        try:
            section.emptyTrash()
        except Exception as e:
            print(f"Could not empty trash after deletion: {e}")
        
        # 3. Sync with Radarr/Sonarr (Unmonitor)
        if tmdb_id:
            unmonitor_in_automation(tmdb_id, "radarr")
        if tvdb_id:
            unmonitor_in_automation(tvdb_id, "sonarr")

    except Exception as e:
        if "403" in str(e) or "400" in str(e):
            raise Exception(f"Plex denied deletion. Ensure 'Allow media deletion' is enabled and you are the server owner. Error: {e}")
        raise Exception(f"Failed to delete from Plex: {e}")
    
    # Delete from local cache; also register the key so a concurrent background
    # refresh doesn't re-insert this item when it replaces library_cache.
    with _data_lock:
        _recently_deleted_keys.add(str(rating_key))
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM library_cache WHERE rating_key = ?", (str(rating_key),))
    conn.commit()
    conn.close()

def fetch_tmdb_movie_collection(row, tmdb_api_key):
    tmdb_id = row['tmdb_id']
    try:
        res = _tmdb_get(f"https://api.themoviedb.org/3/movie/{tmdb_id}?api_key={tmdb_api_key}")
        if res and res.status_code == 200:
            data = res.json()
            collection = data.get('belongs_to_collection')
            if collection:
                col_id = f"movie_{collection['id']}"
                col_name = collection['name']

                col_res = _tmdb_get(f"https://api.themoviedb.org/3/collection/{collection['id']}?api_key={tmdb_api_key}")
                if col_res and col_res.status_code == 200:
                    col_data = col_res.json()
                    parts = col_data.get('parts', [])

                    enriched_parts = []

                    def fetch_part_status(part):
                        try:
                            p_res = _tmdb_get(f"https://api.themoviedb.org/3/movie/{part['id']}?api_key={tmdb_api_key}")
                            if p_res and p_res.status_code == 200:
                                part['status'] = p_res.json().get('status', 'Unknown')
                            else:
                                part['status'] = 'Unknown'
                        except Exception as e:
                            print(f"Error fetching status for part {part.get('id', 'unknown')}: {e}")
                            part['status'] = 'Unknown'
                        return part

                    with ThreadPoolExecutor(max_workers=6) as part_executor:
                        enriched_parts = list(part_executor.map(fetch_part_status, parts))

                    return {
                        'type': 'movie',
                        'tmdb_id': tmdb_id,
                        'collection_id': col_id,
                        'name': col_name,
                        'parts': json.dumps(enriched_parts),
                        'status': 'Released'
                    }
    except Exception as e:
        print(f"Error fetching TMDB movie data for {row['title']}: {e}")
    return None

def fetch_tmdb_tv_status(row, tmdb_api_key):
    tmdb_id = row['tmdb_id']
    try:
        res = _tmdb_get(f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={tmdb_api_key}")
        if res and res.status_code == 200:
            data = res.json()
            status = data.get('status', 'Unknown')
            col_id = f"tv_{tmdb_id}"

            tvdb_id = None
            ext_res = _tmdb_get(f"https://api.themoviedb.org/3/tv/{tmdb_id}/external_ids?api_key={tmdb_api_key}")
            if ext_res and ext_res.status_code == 200:
                tvdb_id = ext_res.json().get('tvdb_id')
            
            return {
                'type': 'show',
                'tmdb_id': tmdb_id,
                'collection_id': col_id,
                'name': row['title'],
                'parts': "[]",
                'status': status,
                'tvdb_id': str(tvdb_id) if tvdb_id else None
            }
    except Exception as e:
        print(f"Error fetching TMDB TV data for {row['title']}: {e}")
    return None

def enrich_series_data(tmdb_api_key, progress_callback=None, target_collection_id=None):
    """Identifies collections/series for movies and TV shows using TMDB API.
    Can be targeted to a single collection_id for a quick refresh.
    """
    if not tmdb_api_key:
        return
    
    conn = sqlite3.connect(DB_PATH)
    
    # Identify what needs updating
    seven_days_ago = (datetime.now().timestamp() - (7 * 24 * 60 * 60))
    
    movie_query = "SELECT lc.* FROM library_cache lc LEFT JOIN series_cache sc ON lc.tmdb_collection_id = sc.collection_id WHERE lc.type = 'movie' AND lc.tmdb_id IS NOT NULL"
    show_query = "SELECT lc.* FROM library_cache lc LEFT JOIN series_cache sc ON lc.tmdb_collection_id = sc.collection_id WHERE lc.type = 'show' AND lc.tmdb_id IS NOT NULL"
    
    if target_collection_id:
        movie_query += " AND lc.tmdb_collection_id = ?"
        show_query += " AND lc.tmdb_collection_id = ?"
        params = (target_collection_id,)
    else:
        movie_query += " AND (lc.tmdb_collection_id IS NULL OR sc.last_updated IS NULL OR strftime('%s', sc.last_updated) < ?)"
        show_query += " AND (lc.tmdb_collection_id IS NULL OR sc.last_updated IS NULL OR strftime('%s', sc.last_updated) < ?)"
        params = (str(int(seven_days_ago)),)
    
    df_movies = pd.read_sql(movie_query, conn, params=params)
    df_shows = pd.read_sql(show_query, conn, params=params)
    
    total_tasks = len(df_movies) + len(df_shows)
    if total_tasks == 0:
        if progress_callback: progress_callback("All series data is up to date.", 1.0)
        conn.close()
        return

    results = []

    # Deduplicate: for movies that already share a known collection_id, fetch once
    seen_collection_ids = set()
    deduped_movies = []
    for _, row in df_movies.iterrows():
        col_id = row.get('tmdb_collection_id')
        if col_id and not pd.isna(col_id):
            if col_id in seen_collection_ids:
                continue
            seen_collection_ids.add(col_id)
        deduped_movies.append(row)

    total_tasks = len(deduped_movies) + len(df_shows)
    if total_tasks == 0:
        if progress_callback: progress_callback("All series data is up to date.", 1.0)
        conn.close()
        return

    with ThreadPoolExecutor(max_workers=4) as executor:
        # Submit Movie tasks (deduplicated)
        future_to_movie = {executor.submit(fetch_tmdb_movie_collection, row, tmdb_api_key): row for row in deduped_movies}
        # Submit TV tasks
        future_to_show = {executor.submit(fetch_tmdb_tv_status, row, tmdb_api_key): row for _, row in df_shows.iterrows()}
        
        all_futures = {**future_to_movie, **future_to_show}
        
        for idx, future in enumerate(as_completed(all_futures)):
            row = all_futures[future]
            if progress_callback:
                progress_callback(f"({idx+1}/{total_tasks}) Scanning TMDB: {row['title']}", (idx + 1) / total_tasks)
            
            res = future.result()
            if res:
                results.append(res)

    # Batch Update DB
    if results:
        c = conn.cursor()
        for res in results:
            # Update library_cache
            c.execute("UPDATE library_cache SET tmdb_collection_id = ? WHERE tmdb_id = ?", 
                     (res['collection_id'], res['tmdb_id']))
            # Update series_cache
            if res['type'] == 'show':
                c.execute("INSERT OR REPLACE INTO series_cache (collection_id, name, parts, status, last_updated, tvdb_id) VALUES (?, ?, ?, ?, ?, ?)",
                         (res['collection_id'], res['name'], res['parts'], res['status'], datetime.now().isoformat(), res['tvdb_id']))
            else:
                c.execute("INSERT OR REPLACE INTO series_cache (collection_id, name, parts, status, last_updated) VALUES (?, ?, ?, ?, ?)",
                         (res['collection_id'], res['name'], res['parts'], res['status'], datetime.now().isoformat()))
        conn.commit()
            
    conn.close()

def toggle_series_visibility(collection_id, hide=True):
    """Marks a series as hidden or visible."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE series_cache SET is_hidden = ? WHERE collection_id = ?", (1 if hide else 0, collection_id))
    conn.commit()
    conn.close()

def get_series_audit_data():
    """Returns a list of series and their status (owned vs missing). 
    Also cleans up series that no longer have any items in the library.
    """
    conn = sqlite3.connect(DB_PATH)
    series_df = pd.read_sql("SELECT * FROM series_cache", conn)
    library_df = pd.read_sql("SELECT tmdb_id, tvdb_id, title, tmdb_collection_id, rating_key, server_id, last_watched_at, viewed_leaf_count, leaf_count, type, size_bytes, library FROM library_cache", conn)
    
    audit_results = []
    collections_to_delete = []
    
    for _, series in series_df.iterrows():
        try:
            parts = json.loads(series['parts'])
        except (json.JSONDecodeError, TypeError) as e:
            print(f"Error parsing parts for series {series['collection_id']}: {e}")
            parts = []
        
        # Determine if we own ANY parts of this series by checking tmdb_id against library_df
        part_ids = [str(p['id']) for p in parts]
        owned_matches = library_df[library_df['tmdb_id'].isin(part_ids)]
        
        # If we own ZERO items from this series, it shouldn't be in our audit list
        if owned_matches.empty:
            # For TV shows, the collection_id is just "tv_{tmdb_id}", 
            # so we check if that specific show is still in the library
            if series['collection_id'].startswith('tv_'):
                show_tmdb_id = series['collection_id'].split('tv_')[-1]
                if library_df[library_df['tmdb_id'] == show_tmdb_id].empty:
                    collections_to_delete.append(series['collection_id'])
                    continue
            else:
                collections_to_delete.append(series['collection_id'])
                continue

        all_parts = []
        owned_count = 0
        
        for part in parts:
            p_id = str(part['id'])
            match = library_df[library_df['tmdb_id'] == p_id]
            is_owned = not match.empty
            
            item_data = {
                'title': part.get('title', part.get('name', 'Unknown')),
                'release_date': part.get('release_date', part.get('first_air_date', '')),
                'status': part.get('status', 'Unknown'),
                'tmdb_id': p_id,
                'overview': part.get('overview', ''),
                'poster_path': part.get('poster_path', ''),
                'is_owned': is_owned
            }
            
            if is_owned:
                owned_count += 1
                item_row = match.iloc[0]
                item_data['rating_key'] = item_row['rating_key']
                item_data['server_id'] = item_row['server_id']
                item_data['last_watched_at'] = item_row['last_watched_at']
                item_data['viewed_leaf_count'] = item_row['viewed_leaf_count']
                item_data['leaf_count'] = item_row['leaf_count']
                item_data['type'] = item_row['type']
                item_data['tvdb_id'] = item_row['tvdb_id']
                item_data['size_bytes'] = int(item_row['size_bytes'] or 0)
                item_data['library'] = item_row['library']
            
            all_parts.append(item_data)
        
        # Sort parts by release date
        all_parts.sort(key=lambda x: x['release_date'] if x['release_date'] else '9999-99-99')
        
        audit_results.append({
            'name': series['name'],
            'collection_id': series['collection_id'],
            'status': series.get('status', 'Unknown'),
            'is_hidden': bool(series.get('is_hidden', 0)),
            'tvdb_id': series.get('tvdb_id'), # Show level ID for Sonarr
            'owned_count': owned_count,
            'total_count': len(parts),
            'parts': all_parts
        })
    
    # Perform cleanup of dead series
    if collections_to_delete:
        c = conn.cursor()
        placeholders = ', '.join(['?'] * len(collections_to_delete))
        c.execute(f"DELETE FROM series_cache WHERE collection_id IN ({placeholders})", collections_to_delete)
        conn.commit()
        
    conn.close()
    return audit_results

# --- Deletion Log ---

def log_deletion(title, type_, library, size_bytes, rating_key):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO deletion_log (deleted_at, title, type, library, size_bytes, rating_key) VALUES (?, ?, ?, ?, ?, ?)",
        (datetime.now().isoformat(), title, type_, library, size_bytes, rating_key)
    )
    conn.commit()
    conn.close()

def get_deletion_log():
    conn = sqlite3.connect(DB_PATH)
    try:
        df = pd.read_sql("SELECT * FROM deletion_log ORDER BY deleted_at DESC LIMIT 1000", conn)
    except Exception:
        df = pd.DataFrame()
    conn.close()
    return df

def clear_deletion_log():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM deletion_log")
    conn.commit()
    conn.close()

# --- Rule Presets ---

def list_presets():
    raw = get_setting('_preset_names')
    return json.loads(raw) if raw else []

def save_preset(name, conditions):
    names = list_presets()
    if name not in names:
        names.append(name)
    save_setting('_preset_names', json.dumps(names))
    save_setting(f'_preset_{name}', json.dumps(conditions))

def load_preset(name):
    raw = get_setting(f'_preset_{name}')
    return json.loads(raw) if raw else []

def delete_preset(name):
    names = [n for n in list_presets() if n != name]
    save_setting('_preset_names', json.dumps(names))
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM settings WHERE key = ?", (f'_preset_{name}',))
    conn.commit()
    conn.close()

# --- Tautulli ---

def test_tautulli(url, api_key):
    try:
        res = requests.get(f"{url}/api/v2", params={"apikey": api_key, "cmd": "get_server_info"}, timeout=10)
        return res.status_code == 200 and res.json().get("response", {}).get("result") == "success"
    except Exception:
        return False

def get_tautulli_users(url=None, api_key=None):
    url = url or get_setting("tautulli_url")
    api_key = api_key or get_setting("tautulli_api_key")
    if not url or not api_key:
        return []
    try:
        res = requests.get(f"{url}/api/v2", params={"apikey": api_key, "cmd": "get_users"}, timeout=10)
        if res.status_code == 200:
            users = res.json().get("response", {}).get("data", [])
            return [
                {"user_id": str(u["user_id"]), "username": u.get("friendly_name") or u.get("username", f"user_{u['user_id']}")}
                for u in users if u.get("user_id")
            ]
    except Exception as e:
        print(f"Tautulli error fetching users: {e}")
    return []

def get_tautulli_watched_keys(user_id, url=None, api_key=None):
    url = url or get_setting("tautulli_url")
    api_key = api_key or get_setting("tautulli_api_key")
    if not url or not api_key:
        return frozenset()
    try:
        res = requests.get(
            f"{url}/api/v2",
            params={"apikey": api_key, "cmd": "get_history", "user_id": user_id, "length": 10000},
            timeout=15
        )
        if res.status_code == 200:
            rows = res.json().get("response", {}).get("data", {}).get("data", [])
            return frozenset(str(r["rating_key"]) for r in rows if r.get("rating_key"))
    except Exception as e:
        print(f"Tautulli error fetching history: {e}")
    return frozenset()

# --- Resolution Duplicates ---

def get_resolution_duplicates():
    """Returns lower-res items where a higher-res copy of the same tmdb_id exists in the library."""
    conn = sqlite3.connect(DB_PATH)
    try:
        df = pd.read_sql(
            "SELECT * FROM library_cache WHERE tmdb_id IS NOT NULL AND tmdb_id != ''",
            conn
        )
    except Exception:
        conn.close()
        return pd.DataFrame()
    conn.close()

    if df.empty:
        return df

    res_rank = {'4k': 4, '1080': 3, '720': 2, 'sd': 1, '': 0}
    df['_res_rank'] = df['resolution'].fillna('').str.lower().map(lambda r: res_rank.get(r, 0))
    best = df.groupby('tmdb_id')['_res_rank'].max().rename('_best_rank')
    df = df.join(best, on='tmdb_id')
    dupes = df[df['_res_rank'] < df['_best_rank']].drop(columns=['_res_rank', '_best_rank'])
    return dupes


init_db()
