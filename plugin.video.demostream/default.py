import sys
import os
import urllib.parse
import xbmc
import xbmcvfs
import xbmcgui
import xbmcplugin
import json
import xbmcaddon
import urllib.request
import re
import hashlib
from datetime import datetime, timedelta
from md5crypt import md5crypt
import xml.etree.ElementTree as ET

addon_dir = os.path.dirname(__file__)
sys.path.append(os.path.join(addon_dir, 'resources', 'lib', 'python'))

from bson import ObjectId
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure
from urllib.parse import parse_qsl
from urllib import request, parse
from tmdb.searchTMDBMulti import search_movie_tmdb
from resources.lib.webshare_client import WebshareClient
from resources.lib.csfd_client import fetch_csfd_tip_titles
from redis_cache import redis_cache

try:
    from xbmc import translatePath
except ImportError:
    from xbmcvfs import translatePath

# Načítanie nastavení doplnku
addon = xbmcaddon.Addon()
mongodb_user = addon.getSetting("mongodb_user")
mongodb_pass = addon.getSetting("mongodb_pass")
mongodb_host = addon.getSetting("mongodb_host")
mongodb_db   = addon.getSetting("mongodb_db")
webshare_user = addon.getSetting("webshare_user")
webshare_pass = addon.getSetting("webshare_pass")

# Skontroluj chýbajúce
missing_settings = []
if not mongodb_user: missing_settings.append("MongoDB - používateľ")
if not mongodb_pass: missing_settings.append("MongoDB - heslo")
if not webshare_user: missing_settings.append("Webshare - používateľ")
if not webshare_pass: missing_settings.append("Webshare - heslo")

if missing_settings:
    xbmcgui.Dialog().ok(
        "Chýbajúce nastavenia",
        "Niektoré údaje nie sú vyplnené:\n" + "\n".join(missing_settings) +
        "\n\nOtvoríme nastavenia doplnku."
    )
    addon.openSettings()
    xbmcplugin.endOfDirectory(int(sys.argv[1]), succeeded=True)
    sys.exit()

# Základné premenné
ADDON_HANDLE = int(sys.argv[1])
BASE_URL = sys.argv[0]
ARGS = urllib.parse.parse_qs(sys.argv[2][1:])

profile_dir = translatePath(addon.getAddonInfo('profile'))
STORAGE_DIR = os.path.join(profile_dir, 'store')
SEARCH_HISTORY_FILE = os.path.join(STORAGE_DIR, 'search_terms.json')
MOVIE_HISTORY_FILE = os.path.join(STORAGE_DIR, 'movie_history.json')
SERIE_HISTORY_FILE = os.path.join(STORAGE_DIR, 'serie_history.json')
PER_PAGE = addon.getSettingInt("per_page") or 20

# Webshare client
ws = WebshareClient()

# Priorita rozlíšení
resolution_order = {
    "4K": 5,
    "FHD": 4,
    "HD": 3,
    "SD": 2,
    "480p": 1
}

# MongoDB Connection Manager
class MongoDBConnection:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialize()
        return cls._instance

    def _initialize(self):
        try:
            uri = f"mongodb://{mongodb_user}:{mongodb_pass}@{mongodb_host}/{mongodb_db}?authSource={mongodb_db}"
            self.client = MongoClient(uri, maxPoolSize=50, connectTimeoutMS=5000, socketTimeoutMS=30000)
            self.db = self.client[mongodb_db]
        except Exception as e:
            xbmc.log(f"[SC3] MongoDB init error: {e}", xbmc.LOGERROR)
            raise e

    def get_db(self):
        try:
            self.client.admin.command('ping')
            return self.db
        except ConnectionFailure as e:
            xbmc.log(f"[SC3] MongoDB ping failed: {e}", xbmc.LOGERROR)
            raise e

    def close(self):
        if self.client:
            self.client.close()
        self._instance = None

# Bezpečné pripojenie k databáze
try:
    db_connection = MongoDBConnection()
    db = db_connection.get_db()
except Exception as e:
    xbmcgui.Dialog().ok("Chyba MongoDB", "Nepodarilo sa pripojiť do databázy.\nSkontrolujte údaje v nastaveniach doplnku.")
    xbmc.log(f"[SC3] MongoDB Connection Error: {e}", xbmc.LOGERROR)
    sys.exit()

# Pomocná funkcia na získanie kolekcie
def get_collection(collection_name):
    return db[collection_name]

# Načítanie žánrov z databázy
genre_dict = {
    str(g["id"]): g["name"]
    for g in get_collection("movie_genres").find()
}

# Vytvorenie úložného priečinka
os.makedirs(STORAGE_DIR, exist_ok=True)

def format_time(seconds):
    mins, secs = divmod(int(seconds), 60)
    return f"{mins}:{secs:02d}"

def create_list_item_legacy(title, plot, year, thumb_url, extra_info={}):
    li = xbmcgui.ListItem(label=title)
    info = {
        "title": title,
        "plot": plot,
        "year": int(year) if str(year).isdigit() else None,
        "mediatype": "episode"
    }
    info.update(extra_info)
    li.setInfo("video", info)
    li.setArt({'thumb': thumb_url, 'poster': thumb_url, 'fanart': thumb_url})
    li.setProperty("IsPlayable", "true")
    return li

def create_list_item_modern(title, plot, year, thumb_url, extra_info={}):
    li = xbmcgui.ListItem(label=title)
    try:
        tag = li.getVideoInfoTag()
        tag.setTitle(title)
        tag.setPlot(plot)
        if str(year).isdigit():
            tag.setYear(int(year))
        tag.setMediaType("episode")
    except Exception as e:
        xbmc.log(f"[SC3] Fallback InfoTag: {e}", xbmc.LOGDEBUG)
        li.setInfo("video", {
            "title": title,
            "plot": plot,
            "year": int(year) if str(year).isdigit() else None,
            "mediatype": "episode"
        })
    li.setArt({'thumb': thumb_url, 'poster': thumb_url, 'fanart': thumb_url})
    li.setProperty("IsPlayable", "true")
    return li

def create_compatible_list_item(title, plot, year, thumb_url, extra_info={}):
    try:
        kodi_major = int(xbmc.getInfoLabel("System.BuildVersion").split(".")[0])
    except:
        kodi_major = 18
    if kodi_major >= 20:
        return create_list_item_modern(title, plot, year, thumb_url, extra_info)
    else:
        return create_list_item_legacy(title, plot, year, thumb_url, extra_info)

#-------- Pridanie položky do zoznamu pre movies --------
def add_movie_listitem(movie, addon_handle):
    title = movie.get("title")
    year = movie.get("year")
    movie_id = movie.get("movieId")
    poster_url = movie.get("posterUrl")
    overview = movie.get("overview", "Žiadny popis k dispozícii.")
    vote = movie.get("vote_average")
    release_date = movie.get("release_date")

    url = build_url({'action': 'select_stream', 'movieId': movie_id})

    # Načítanie jazykov
    languages_str = ""
    try:
        audio_languages = get_collection("movie_detail").find({"fkMovieId": movie_id}).distinct("audio")
        processed_languages = set()
        for lang_entry in audio_languages:
            if isinstance(lang_entry, list):
                for lang in lang_entry:
                    if lang:
                        lang_code = re.split(r'[\(\)]', lang)[0].strip().upper()
                        processed_languages.add(lang_code if lang_code != "UND" else "UND")
            elif isinstance(lang_entry, str):
                lang_code = re.split(r'[\(\)]', lang_entry)[0].strip().upper()
                processed_languages.add(lang_code if lang_code != "UND" else "UND")

        if processed_languages:
            languages_str = f" [COLOR deepskyblue]{', '.join(sorted(processed_languages))}[/COLOR]"

    except Exception as e:
        xbmc.log(f"Chyba pri získavaní jazykov pre movieId {movie_id}: {str(e)}", xbmc.LOGWARNING)

    # ❗ Získanie žánrov zo zoznamu ID
    genre_names = [genre_dict.get(str(gid), f"Žáner {gid}") for gid in movie.get("genres", [])]

    # Formátovaný label
    label = f"[B]{title}[/B]"
    if year:
        label += f" [COLOR grey]({year})[/COLOR]"
    if vote:
        label += f" [COLOR gold]{vote:.1f}★[/COLOR]"
    label += languages_str

    # Video info
    info = {
        'title': title,
        'year': int(year) if year and str(year).isdigit() else None,
        'plot': overview,
        'rating': float(vote) if vote else None,
        'premiered': release_date if release_date else None,
        'genre': ", ".join(genre_names) if genre_names else None
    }

    li = xbmcgui.ListItem(label=label)
    li.setInfo('video', info)
    # Poster / Fanart / Thumb
    art = {
        'thumb': poster_url or 'DefaultVideo.png',
        'poster': poster_url or 'DefaultVideo.png',
        'fanart': poster_url or 'DefaultVideo.png'
    }
    li.setArt(art)
    li.setProperty('movie_id', str(movie_id))
    li.setProperty('IsPlayable', 'true')
    xbmcplugin.addDirectoryItem(handle=addon_handle, url=url, listitem=li, isFolder=False)

#-------- Pridanie položky do zoznamu pre seriály --------
def add_series_listitem(series, addon_handle):
    title = series.get("title", "Neznámy názov")
    first_air_date = series.get("first_air_date")  # očakávame rok ako int alebo string
    last_air_date = series.get("last_air_date")
    serie_id = series.get("serieId")
    poster_url = series.get("posterUrl")
    overview = series.get("overview", "Žiadny popis k dispozícii.")

    url = build_url({'action': 'list_seasons', 'serieId': serie_id})

    # Formátovanie rokov
    year_label = ""
    try:
        # Pokúsime sa získať len rok (4 číslice) z dátumu, ak je to string, alebo použiť priamo int
        def extract_year(value):
            if value is None:
                return None
            if isinstance(value, int):
                return value
            if isinstance(value, str):
                # napr. '2021-09-15' alebo len '2021'
                return int(value[:4])
            return None

        first_year = extract_year(first_air_date)
        last_year = extract_year(last_air_date)

        if first_year and last_year:
            if first_year == last_year:
                year_label = f"{first_year}"
            else:
                year_label = f"{first_year} - {last_year}"
        elif first_year:
            year_label = f"{first_year}"
        elif last_year:
            year_label = f"{last_year}"
    except Exception:
        year_label = ""

    # Formátovaný label
    label = f"[B]{title}[/B]"
    if year_label:
        label += f" [COLOR grey]({year_label})[/COLOR]"

    li = xbmcgui.ListItem(label=label)
    li.setInfo('video', {
        'title': title,
        'year': first_year or last_year,  # pre metadata stačí jeden rok
        'plot': overview,
        'mediatype': 'tvshow'
    })
    li.setArt({'thumb': poster_url} if poster_url else {'thumb': 'DefaultTVShows.png'})
    xbmcplugin.addDirectoryItem(handle=addon_handle, url=url, listitem=li, isFolder=True)

def add_pagination_controls(page, total_count, per_page, query=None, action='show_movies'):
    # Hlavné menu
    url = build_url({'action': 'main_menu'})
    li = xbmcgui.ListItem(label='[B][COLOR white]<< Prejdi na hlavné menu[/COLOR][/B]')
    li.setArt({'icon': 'DefaultFolderBack.png'})
    xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=url, listitem=li, isFolder=True)

    # Predošlá strana
    if page > 1:
        prev_params = {'action': action, 'page': page - 1}
        if query:
            prev_params['query'] = query
        url = build_url(prev_params)
        li = xbmcgui.ListItem(label='[B][COLOR white]< Predošlá strana[/COLOR][/B]')
        li.setArt({'icon': 'DefaultFolderBack.png'})
        xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=url, listitem=li, isFolder=True)

    # Ďalšia strana
    if total_count == per_page:
        next_params = {'action': action, 'page': page + 1}
        if query:
            next_params['query'] = query
        url = build_url(next_params)
        li = xbmcgui.ListItem(label='[B][COLOR white]>> Ďalšia strana[/COLOR][/B]')
        li.setArt({'icon': 'DefaultFolderForward.png'})
        xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=url, listitem=li, isFolder=True)

# -------- História vyhľadávania --------
def save_search_term(term):
    history = []
    if os.path.exists(SEARCH_HISTORY_FILE):
        with open(SEARCH_HISTORY_FILE, 'r', encoding='utf-8') as f:
            try:
                history = json.load(f)
            except json.JSONDecodeError:
                history = []
    if term in history:
        history.remove(term)
    history.insert(0, term)
    history = history[:20]
    with open(SEARCH_HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(history, f)

def load_search_terms():
    if not os.path.exists(SEARCH_HISTORY_FILE):
        return []
    with open(SEARCH_HISTORY_FILE, 'r', encoding='utf-8') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []

# -------- História prehrávaní filmov --------
def save_played_movie(item_id):
    history = []
    if os.path.exists(MOVIE_HISTORY_FILE):
        with open(MOVIE_HISTORY_FILE, 'r', encoding='utf-8') as f:
            try:
                history = json.load(f)
            except json.JSONDecodeError:
                history = []
    if item_id in history:
        history.remove(item_id)
    history.insert(0, item_id)
    history = history[:20]
    with open(MOVIE_HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(history, f)

# -------- História prehrávaní seriálov --------
def save_played_series(item_id):
    history = []
    if os.path.exists(SERIE_HISTORY_FILE):
        with open(SERIE_HISTORY_FILE, 'r', encoding='utf-8') as f:
            try:
                history = json.load(f)
            except json.JSONDecodeError:
                history = []
    if item_id in history:
        history.remove(item_id)
    history.insert(0, item_id)
    history = history[:20]
    with open(SERIE_HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(history, f)

#-------- Načítanie histórie prehrávaní filmov --------
def load_played_movies():
    if not os.path.exists(MOVIE_HISTORY_FILE):
        return []
    with open(MOVIE_HISTORY_FILE, 'r', encoding='utf-8') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []

#-------- Načítanie histórie prehrávaní seriálov --------
def load_played_series():
    if not os.path.exists(SERIE_HISTORY_FILE):
        return []
    with open(SERIE_HISTORY_FILE, 'r', encoding='utf-8') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []

# --------- Mazanie historie --------
def clear_played_history():
    movie_deleted = False
    series_deleted = False

    if os.path.exists(MOVIE_HISTORY_FILE):
        os.remove(MOVIE_HISTORY_FILE)
        movie_deleted = True

    if os.path.exists(SERIE_HISTORY_FILE):
        os.remove(SERIE_HISTORY_FILE)
        series_deleted = True

    if movie_deleted or series_deleted:
        xbmcgui.Dialog().notification("Hotovo", "Naposledy sledované bolo vymazané.", xbmcgui.NOTIFICATION_INFO, 3000)
    else:
        xbmcgui.Dialog().notification("Upozornenie", "Zoznamy sú už prázdne.", xbmcgui.NOTIFICATION_INFO, 3000)

# --------- Mazanie historie hľadania --------
def clear_search_history():
    if os.path.exists(SEARCH_HISTORY_FILE):
        os.remove(SEARCH_HISTORY_FILE)
        xbmcgui.Dialog().notification("Hotovo", "História hľadania bola vymazaná.", xbmcgui.NOTIFICATION_INFO, 3000)
    else:
        xbmcgui.Dialog().notification("Upozornenie", "História je už prázdna.", xbmcgui.NOTIFICATION_INFO, 3000)

# -------- Ostatné funkcie --------
def build_url(query):
    return BASE_URL + '?' + urllib.parse.urlencode(query)

def main_menu():
    """Zobrazenie hlavného menu"""
    xbmcplugin.setPluginCategory(ADDON_HANDLE, 'Hlavné menu')
    xbmcplugin.setContent(ADDON_HANDLE, 'videos')

    # Vytvorenie položiek menu
    menu_items = [
        ('Vyhľadávanie', 'search', 'DefaultAddonsSearch.png'),
        ('Najnovšie filmy', 'show_latest_movies', 'DefaultRecentlyAddedMovies.png'),
        ('Novinky dabované', 'show_latest_dubbed_movies', 'DefaultRecentlyAddedMovies.png'),
        ('Najnovšie seriály', 'list_latest_series', 'DefaultRecentlyAddedEpisodes.png'),
        ('Naposledy pridané filmy', 'show_latest_added_movies', 'DefaultRecentlyAddedMovies.png'),
        ('Naposledy pridané seriály', 'list_latest_added_series', 'DefaultRecentlyAddedEpisodes.png'),
        ('Tipy na dnes (ČSFD)', 'typy_na_dnes_csfd', 'DefaultTVShows.png'),
        ('Trending filmy (posledných 14 dní) TMDB', 'list_trending_movies_last_14_days', 'DefaultMovies.png'),
        ('Top 100 populárnych filmov (CZ/SK) TMDB', 'list_top_popular_movies_czsk', 'DefaultMovies.png'),
        ('Top 100 najlepšie hodnotených filmov (CZ/SK)', 'list_top_rated_movies_czsk', 'DefaultMovies.png'),
        ('Filmy', 'show_movies', 'DefaultVideo.png'),
        ('Seriály', 'list_series', 'DefaultTVShows.png'),
        ('Filmy podľa názvu (A-Z)', 'list_movies_by_name', 'DefaultVideo.png'),
        ('Seriály podľa názvu (A-Z)', 'list_series_by_name', 'DefaultTVShows.png'),
        ('Naposledy hľadané', 'recent_searches', 'DefaultFolder.png'),
        ('Naposledy sledované filmy', 'recently_played', 'DefaultPlaylist.png'),
        ('Naposledy sledované seriály', 'recently_played_series', 'DefaultPlaylist.png'),
        ('Vymazať históriu hľadania', 'clear_search_history', 'DefaultVideoDeleted.png'),
        ('Vymazať naposledy sledované zoznamy', 'clear_played_history', 'DefaultVideoDeleted.png')
    ]

    # Pridanie položiek do menu
    for label, action, icon in menu_items:
        url = build_url({'action': action})
        li = xbmcgui.ListItem(label=label)
        li.setArt({'icon': icon, 'thumb': icon})
        xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=url, listitem=li, isFolder=action not in ['clear_search_history', 'clear_played_history'])

    xbmcplugin.endOfDirectory(ADDON_HANDLE)

def search_movies():
    keyboard = xbmc.Keyboard('', 'Zadaj názov filmu alebo seriálu')
    keyboard.doModal()

    if keyboard.isConfirmed():
        search_text = keyboard.getText().strip()
        if not search_text:
            xbmcgui.Dialog().notification("Chyba", "Nebolo zadané nič na vyhľadanie.", xbmcgui.NOTIFICATION_ERROR, 3000)
            main_menu()
            return
        save_search_term(search_text)
        process_tmdb_search_results(search_text)
    else:
        xbmc.log("Hľadanie bolo zrušené (ESC alebo klik na Cancel).", xbmc.LOGINFO)
        main_menu()

def process_tmdb_search_results(query, page=1):
    """Process TMDB search results and verify against our database"""
    xbmcplugin.setPluginCategory(ADDON_HANDLE, 'Výsledky vyhľadávania')
    xbmcplugin.setContent(ADDON_HANDLE, 'videos')

    # Add "Go to main menu" as first item
    url = build_url({'action': 'main_menu'})
    li = xbmcgui.ListItem(label='[B]<< Prejdi na hlavné menu[/B]')
    li.setArt({'icon': 'DefaultFolderBack.png'})
    xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=url, listitem=li, isFolder=True)

    # Get TMDB results
    tmdb_results = search_movie_tmdb(query)
    if not tmdb_results:
        xbmcgui.Dialog().notification("Info", "Nenašli sa žiadne výsledky.", xbmcgui.NOTIFICATION_INFO, 3000)
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return

    # Verify against our DB and create combined results
    movies_found = []
    series_found = []

    for item in tmdb_results:
        tmdb_id = item['tmdbId']
        media_type = item['mediaType']

        if media_type == "movie":
            # Check movies collection
            movie = get_collection("movies").find_one({"tmdbId": tmdb_id, "status": 1})
            if movie:
                movies_found.append(movie)
        elif media_type == "tv":
            # Check series collection
            series = get_collection("series").find_one({"tmdbId": tmdb_id})
            if series:
                series_found.append(series)

    # Pagination
    skip_count = (page - 1) * PER_PAGE
    all_results = movies_found + series_found
    total_results = len(all_results)

    if not all_results:
        xbmcgui.Dialog().notification("Info", "Nenašli sa žiadne výsledky v tvojej knižnici.", xbmcgui.NOTIFICATION_INFO, 3000)
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return

    # Add "Previous page" if needed
    if page > 1:
        prev_page_url = build_url({'action': 'search_results', 'query': query, 'page': page - 1})
        li = xbmcgui.ListItem(label='[B]< Predošlá strana[/B]')
        li.setArt({'icon': 'DefaultFolderBack.png'})
        xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=prev_page_url, listitem=li, isFolder=True)

    # Display movies
    for movie in movies_found[skip_count:skip_count + PER_PAGE]:
        add_movie_listitem(movie, ADDON_HANDLE)

    # Display series
    for series in series_found[skip_count:skip_count + PER_PAGE]:
        add_series_listitem(series, ADDON_HANDLE)

    # Add "Next page" if needed
    if total_results > skip_count + PER_PAGE:
        next_page_url = build_url({'action': 'search_results', 'query': query, 'page': page + 1})
        li = xbmcgui.ListItem(label='[B]>> Ďalšia strana[/B]')
        xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=next_page_url, listitem=li, isFolder=True)

    xbmcplugin.endOfDirectory(ADDON_HANDLE)

def show_movies(query=None, page=1):
    xbmcplugin.setPluginCategory(ADDON_HANDLE, 'Výsledky vyhľadávania' if query else 'Filmy')
    xbmcplugin.setContent(ADDON_HANDLE, 'videos')
    try:

        if query is None:
            mongo_query = {"status": 1}
        elif isinstance(query, str):
            mongo_query = {"title": {"$regex": query, "$options": "i"}, "status": 1}
        elif isinstance(query, dict):
            mongo_query = query
        else:
            mongo_query = {"status": 1}

        skip_count = (page - 1) * PER_PAGE
        
        # bezpečný cache key
        query_str = json.dumps(mongo_query, sort_keys=True)
        query_hash = hashlib.md5(query_str.encode("utf-8")).hexdigest()
        cache_key = f"movies_{query_hash}_{skip_count}_{PER_PAGE}"

        movies = redis_cache.get_or_cache(
            cache_key,
            lambda: list(
                get_collection("movies")
                .find(mongo_query)
                .sort("movieId", -1)
                .skip(skip_count)
                .limit(PER_PAGE)
            ),
            ttl=600
        )
        #movies = get_collection("movies").find(mongo_query).sort("movieId", -1).skip(skip_count).limit(PER_PAGE)

        # Pridaj navigáciu na začiatok
        add_pagination_controls(page, 0, PER_PAGE, query, action='show_movies')

        count = 0
        for movie in movies:
            count += 1
            add_movie_listitem(movie, ADDON_HANDLE)

        # Pridaj navigáciu na koniec, ak treba
        add_pagination_controls(page, count, PER_PAGE, query, action='show_movies')

    except ConnectionFailure:
        xbmc.log("MongoDB connection failed in show_movies", xbmc.LOGERROR)
        xbmcgui.Dialog().notification("Chyba", "Problém s připojením k databázi", xbmcgui.NOTIFICATION_ERROR)
    except Exception as e:
        xbmc.log(f"Error in show_movies: {str(e)}", xbmc.LOGERROR)
    finally:
        xbmcplugin.endOfDirectory(ADDON_HANDLE)

# Zoznam filmov podla datumu vydania
def show_latest_movies(page=1):
    xbmcplugin.setPluginCategory(ADDON_HANDLE, 'Najnovšie filmy')
    xbmcplugin.setContent(ADDON_HANDLE, 'movies')

    skip_count = (page - 1) * PER_PAGE
    
    movies = get_collection("movies").find({"status": 1}).sort("release_date", -1).skip(skip_count).limit(PER_PAGE)

    movies_list = list(movies)

    # Navigácia hore
    add_pagination_controls(page, 0, PER_PAGE, action='show_latest_movies')

    count = 0
    for movie in movies_list:
        count += 1
        add_movie_listitem(movie, ADDON_HANDLE)

    # Navigácia dole
    add_pagination_controls(page, count, PER_PAGE, action='show_latest_movies')

    xbmcplugin.endOfDirectory(ADDON_HANDLE)

# Zoznam filmov podla datumu vydaniapridania
def show_latest_added_movies(page=1):
    xbmcplugin.setPluginCategory(ADDON_HANDLE, 'Najnovšie pridané filmy')
    xbmcplugin.setContent(ADDON_HANDLE, 'movies')

    skip_count = (page - 1) * PER_PAGE

    movies = get_collection("movies").find({"status": 1}).sort("movieId", -1).skip(skip_count).limit(PER_PAGE)

    movies_list = list(movies)

    # Navigácia hore
    add_pagination_controls(page, 0, PER_PAGE, action='show_latest_added_movies')

    count = 0
    for movie in movies_list:
        count += 1
        add_movie_listitem(movie, ADDON_HANDLE)

    # Navigácia dole
    add_pagination_controls(page, count, PER_PAGE, action='show_latest_added_movies')

    xbmcplugin.endOfDirectory(ADDON_HANDLE)

#-------- Najnovšie seriály --------
def list_latest_series():
    xbmcplugin.setPluginCategory(ADDON_HANDLE, 'Najnovšie seriály')
    xbmcplugin.setContent(ADDON_HANDLE, 'tvshows')

    try:

        # Add main menu link
        url = build_url({'action': 'main_menu'})
        li = xbmcgui.ListItem(label='[B]<< Prejdi na hlavné menu[/B]')
        li.setArt({'icon': 'DefaultFolderBack.png'})
        xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=url, listitem=li, isFolder=True)

        # Get latest 50 series with available episodes
        series_with_episodes = get_collection("episodes").aggregate([
            {"$match": {"statusWS": 1}},
            {"$group": {"_id": "$serieId"}},
            {"$lookup": {
                "from": "series",
                "localField": "_id",
                "foreignField": "serieId",
                "as": "seriesInfo"
            }},
            {"$unwind": "$seriesInfo"},
            {"$replaceRoot": {"newRoot": "$seriesInfo"}},
            {"$sort": {"last_air_date": -1}},
            {"$limit": 50}
        ])

        for s in series_with_episodes:
            title = s.get("title", "Neznámy názov")
            year = s.get("year")
            overview = s.get("overview", "")
            poster_url = s.get("posterUrl")
            serie_id = s.get("serieId")
            last_air_date = s.get("last_air_date", "")
            first_air_date = s.get("first_air_date", "")

            year2title = (first_air_date.split("-")[0] + (" - " + last_air_date.split("-")[0]
                  if last_air_date and last_air_date.split("-")[0] != first_air_date.split("-")[0]
                  else "") if first_air_date else "")

            url = build_url({'action': 'list_seasons', 'serieId': serie_id})
            label = f"{title} [B][COLOR white][{year2title}][/COLOR][/B]" if year2title else title

            # Get audio languages
            audio_languages = get_collection("episodes").find({"serieId": serie_id}).distinct("audio")
            if audio_languages:
                processed_languages = set()
                for lang in audio_languages:
                    if lang:
                        lang_code = re.split(r'[\(\)]', lang)[0].strip().upper()
                        if lang_code == "UND":
                            lang_code = "UND"
                        processed_languages.add(lang_code)

                if processed_languages:
                    languages_str = ",".join(sorted(processed_languages))
                    label += f" [B][COLOR white]{languages_str}[/COLOR][/B]"

            li = xbmcgui.ListItem(label=label)
            li.setInfo('video', {
                'title': title,
                'year': year,
                'plot': overview,
                'premiered': first_air_date
            })
            if poster_url:
                li.setArt({'thumb': poster_url})
            xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=url, listitem=li, isFolder=True)

    except ConnectionFailure:
        xbmc.log("MongoDB connection failed in list_latest_series", xbmc.LOGERROR)
        xbmcgui.Dialog().notification("Chyba", "Problém s připojením k databázi", xbmcgui.NOTIFICATION_ERROR)
    except Exception as e:
        xbmc.log(f"Error in list_latest_series: {str(e)}", xbmc.LOGERROR)
    finally:
        xbmcplugin.endOfDirectory(ADDON_HANDLE)

#-------- Najnovšie pridané seriály --------
def list_latest_added_series(page=1):
    xbmcplugin.setPluginCategory(ADDON_HANDLE, 'Najnovšie pridané seriály')
    xbmcplugin.setContent(ADDON_HANDLE, 'tvshows')

    skip_count = (page - 1) * PER_PAGE

    # Get latest 50 series with available episodes
    series_with_episodes = get_collection("episodes").aggregate([
        {"$match": {"statusWS": 1}},
        {"$group": {"_id": "$serieId"}},
        {"$lookup": {
            "from": "series",
            "localField": "_id",
            "foreignField": "serieId",
            "as": "seriesInfo"
        }},
        {"$unwind": "$seriesInfo"},
        {"$replaceRoot": {"newRoot": "$seriesInfo"}},
        {"$sort": {"serieId": -1}},
        {"$skip": skip_count},
        {"$limit": PER_PAGE}
    ])

    series_list = list(series_with_episodes)

    # Navigácia hore
    add_pagination_controls(page, 0, PER_PAGE, action='list_latest_added_series')

    count = 0
    for series in series_list:
        count += 1
        add_series_listitem(series, ADDON_HANDLE)

    # Navigácia dole
    add_pagination_controls(page, count, PER_PAGE, action='list_latest_added_series')

    xbmcplugin.endOfDirectory(ADDON_HANDLE)

# -------- Naposledy hľadané seriály--------
def list_recent_searches():
    xbmcplugin.setPluginCategory(ADDON_HANDLE, 'Naposledy hľadané')
    xbmcplugin.setContent(ADDON_HANDLE, 'videos')

    search_terms = load_search_terms()

    if not search_terms:
        xbmcgui.Dialog().notification("Bez histórie", "Žiadne nedávne vyhľadávania.", xbmcgui.NOTIFICATION_INFO, 3000)
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return

    for term in search_terms:
        if term and isinstance(term, str) and term.strip():  # kontrola na neprázdny reťazec
            url = build_url({'action': 'search', 'query': term})
            li = xbmcgui.ListItem(label=term.strip())
            xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=url, listitem=li, isFolder=True)
        else:
            xbmc.log(f"[SC3] Preskočený neplatný výraz vo vyhľadávacej histórii: {term}", xbmc.LOGWARNING)

    xbmcplugin.endOfDirectory(ADDON_HANDLE)

#-------- Naposledy sledované filmy --------
def list_played_movies():
    xbmcplugin.setPluginCategory(ADDON_HANDLE, 'Naposledy sledované')
    xbmcplugin.setContent(ADDON_HANDLE, 'videos')

    history = load_played_movies()

    if not history:
        xbmcgui.Dialog().notification("Bez záznamov", "Žiadne nedávno prehrávané filmy.", xbmcgui.NOTIFICATION_INFO, 3000)
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return

    for movie_id in history:
        movie = get_collection("movies").find_one({"movieId": movie_id})
        if movie:
            add_movie_listitem(movie, ADDON_HANDLE)

    xbmcplugin.endOfDirectory(ADDON_HANDLE)

#-------- Naposledy sledované seriály --------
def list_played_series():
    xbmcplugin.setPluginCategory(ADDON_HANDLE, 'Naposledy sledované seriály')
    xbmcplugin.setContent(ADDON_HANDLE, 'tvshows')

    history = load_played_series()

    if not history:
        xbmcgui.Dialog().notification("Bez záznamov", "Žiadne nedávno prehrávané seriály.", xbmcgui.NOTIFICATION_INFO, 3000)
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return

    for serie_id in history:
        series = get_collection("series").find_one({"serieId": serie_id})
        if series:  # ← pridaná kontrola
            add_series_listitem(series, ADDON_HANDLE)

    xbmcplugin.endOfDirectory(ADDON_HANDLE)

#  Seriály
def list_series(page=1):
    xbmcplugin.setPluginCategory(ADDON_HANDLE, 'Seriály')
    xbmcplugin.setContent(ADDON_HANDLE, 'tvshows')

    skip_count = (page - 1) * PER_PAGE

    series_with_episodes = get_collection("episodes").aggregate([
        {"$match": {"statusWS": 1}},
        {"$group": {"_id": "$serieId"}},
        {"$lookup": {
            "from": "series",
            "localField": "_id",
            "foreignField": "serieId",
            "as": "seriesInfo"
        }},
        {"$unwind": "$seriesInfo"},
        {"$replaceRoot": {"newRoot": "$seriesInfo"}},
        {"$sort": {"title": 1}},
        {"$skip": skip_count},
        {"$limit": PER_PAGE}
    ])

    series_list = list(series_with_episodes)

    # Navigácia hore
    add_pagination_controls(page, 0, PER_PAGE, action='list_series')

    count = 0
    for series in series_list:
        count += 1
        add_series_listitem(series, ADDON_HANDLE)

    # Navigácia dole
    add_pagination_controls(page, count, PER_PAGE, action='list_series')

    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def list_seasons(serieId):
    xbmcplugin.setPluginCategory(ADDON_HANDLE, 'Sezóny')
    xbmcplugin.setContent(ADDON_HANDLE, 'seasons')

    try:
        serieId = int(serieId)
        
        cache_key = f"seasons_{serieId}"
        all_seasons = redis_cache.get_or_cache(
            cache_key,
            lambda: list(get_collection("seasons").find({"serieId": serieId})),
            ttl=600
        )

        available_counts = list(get_collection("episodes").aggregate([
            {"$match": {"serieId": serieId, "statusWS": 1}},
            {"$group": {"_id": "$seasonId", "count": {"$sum": 1}}}
        ]))
        
        available_dict = {s["_id"]: s["count"] for s in available_counts}

        for season in all_seasons:
            season_id = season["seasonId"]
            season_num = season["season_number"]
            name = season.get("name") or f"Sezóna {season_num}"
            year = season.get("air_date", "")[:4] if season.get("air_date") else ""
            ep_count = available_dict.get(season_id, 0)

            # Label
            label = f"[B]{name}[/B]"
            if year:
                label += f" [COLOR grey]({year})[/COLOR]"
            if ep_count > 0:
                label += f" [COLOR lightgreen]{ep_count} epizód[/COLOR]"

            li = xbmcgui.ListItem(label=label)
            li.setInfo('video', {
                'title': name,
                'plot': season.get("overview", ""),
                'premiered': season.get("air_date", ""),
                'season': season_num,
                'episode': season.get("episode_count", 0),
                'mediatype': 'season'
            })

            if season.get("posterUrl"):
                li.setArt({
                    'thumb': season["posterUrl"],
                    'poster': season["posterUrl"],
                    'fanart': season["posterUrl"]
                })

            url = build_url({'action': 'list_episodes', 'serieId': serieId, 'seasonId': season_id})
            xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=url, listitem=li, isFolder=True)

    finally:
        xbmcplugin.endOfDirectory(ADDON_HANDLE)


def list_episodes(serieId, seasonId):
    xbmcplugin.setPluginCategory(ADDON_HANDLE, 'Epizódy')
    xbmcplugin.setContent(ADDON_HANDLE, 'episodes')

    try:
        serieId = int(serieId)
        seasonId = int(seasonId)

        season_data = get_collection("seasons").find_one({"serieId": serieId, "seasonId": seasonId})
        season_num = season_data.get("season_number", 1) if season_data else 1

        episodes = redis_cache.get_or_cache(
            f"episodes_{serieId}_{seasonId}",
            lambda: list(get_collection("episodes").find({
                "serieId": serieId,
                "seasonId": seasonId,
                "statusWS": 1
            }).sort("episode_number", 1)),
            ttl=600
        )

        for episode in episodes:
            ep_num = episode.get("episode_number", 0)
            ep_title = episode.get("name", f"Epizóda {ep_num}")
            aired = episode.get("air_date", "")
            aired_str = ""
            try:
                aired_str = datetime.strptime(aired, '%Y-%m-%d').strftime('%d.%m.%Y') if aired else ""
            except:
                pass

            rating = episode.get("vote_average", 0)
            runtime = episode.get("runtime", 0)

            # Formátovaný label
            label = f"[B]{ep_num}. {ep_title}[/B]"
            if aired_str:
                label += f" [COLOR grey]({aired_str})[/COLOR]"

            details = []
            if runtime:
                details.append(f"{runtime} min")
            if rating:
                details.append(f"{rating}/10")
            if details:
                label += f" [COLOR orange]{' • '.join(details)}[/COLOR]"

            li = xbmcgui.ListItem(label=label)
            li.setInfo('video', {
                'title': ep_title,
                'plot': episode.get("overview", "Bez popisu."),
                'season': season_num,
                'episode': ep_num,
                'aired': aired,
                'rating': rating,
                'votes': episode.get("vote_count", 0),
                'duration': runtime,
                'mediatype': 'episode'
            })

            if episode.get("stillUrl"):
                li.setArt({
                    'thumb': episode["stillUrl"],
                    'poster': episode["stillUrl"],
                    'fanart': episode["stillUrl"]
                })

            url = build_url({'action': 'select_stream_serie', 'episodeId': episode["episodeId"]})
            li.setProperty('IsPlayable', 'true')
            xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=url, listitem=li, isFolder=False)

    finally:
        xbmcplugin.endOfDirectory(ADDON_HANDLE)


def select_stream(movie_id):

    try:
        movie_id = int(movie_id)
    except ValueError:
        xbmc.log(f"movie_id nie je číslo: {movie_id}", xbmc.LOGERROR)
        xbmcgui.Dialog().notification("Chyba", "Neplatné ID filmu.", xbmcgui.NOTIFICATION_ERROR, 3000)
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return

    details = list(get_collection("movie_detail").find({"fkMovieId": movie_id}))

    if not details:
        xbmcgui.Dialog().notification("Žiadne súbory", "Pre tento film nie sú dostupné žiadne súbory.", xbmcgui.NOTIFICATION_INFO, 3000)
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return

    # Sort najprv podľa rozlíšenia (custom poradie), potom podľa veľkosti (ako float)
    details.sort(
        key=lambda x: (
            resolution_order.get(x.get("resolution", ""), 0),  # predvolené 0 ak chýba
            float(x.get("size", "0").replace(" GB", ""))
        ),
        reverse=True  # zoradenie od najvyššieho rozlíšenia a veľkosti
    )

    # Get movie details for thumbnail
    movie_info = get_collection("movies").find_one({"movieId": movie_id})
    default_thumb = movie_info.get('posterUrl', 'DefaultAddonVideo.png')

    dialog = xbmcgui.Dialog()
    items = []

    for file in details:
        audio_streams = file.get("audio", [])
        resolution = file.get("resolution", "N/A")
        size = file.get("size", "N/A")
        bitrate = file.get("bitrate", "N/A")
        video_codec = file.get("videoCodec", "N/A")
        filename = file.get("name", "N/A")

        scrollable_filename = f"[COLOR FFFF00FF]{filename}[/COLOR]" if filename else ""
        li = xbmcgui.ListItem(label=f"[B]{resolution}[/B]  •  [COLOR FF00FF00]{size}[/COLOR]  •  {scrollable_filename}")
        li.setLabel2(f"[COLOR FFFFCC00]{', '.join(audio_streams)}[/COLOR] • {bitrate} • {video_codec}")
        li.setArt({'thumb': default_thumb, 'icon': 'DefaultAddonVideo.png'})

        items.append(li)

    index = dialog.select(
        "Vyberte stream: [COLOR FFFFCC00]Rozlišenie • Veľkosť[/COLOR]",
        items,
        useDetails=True
    )

    if index >= 0:
        selected_ident = details[index].get("ident")
        play_url = ws.get_stream_url(ident=selected_ident, mongo_collection=get_collection("movie_detail"))

        error_messages = {
            "deleted": ("Vymazané", f"Záznam {selected_ident} bol úspešne vymazaný.", xbmcgui.NOTIFICATION_INFO),
            "unauthorized": ("Prístup odmietnutý", "Nemáš práva na mazanie z databázy.", xbmcgui.NOTIFICATION_ERROR),
            "delete_error": ("Chyba", "Mazanie z databázy zlyhalo.", xbmcgui.NOTIFICATION_ERROR),
            "not_found": ("Nenájdené", "Záznam s ident sa nenašiel.", xbmcgui.NOTIFICATION_INFO),
            "password_protected": ("Súbor je zaheslovaný", "Nie je možné ho prehrať.", xbmcgui.NOTIFICATION_ERROR),
            "temporarily_unavailable": ("Dočasne nedostupné", "Súbor je momentálne nedostupný.", xbmcgui.NOTIFICATION_WARNING),
            "non_public_file": ("Súkromný súbor", "Obsah nie je verejný. Môže ísť o autorsky chránené video.", xbmcgui.NOTIFICATION_ERROR),
            None: ("Stream URL", "Nepodarilo sa získať stream URL.", xbmcgui.NOTIFICATION_ERROR)
        }

        if play_url in error_messages:
            title, message, icon = error_messages[play_url]
            xbmcgui.Dialog().notification(title, message, icon)
            xbmcplugin.endOfDirectory(ADDON_HANDLE, succeeded=False)
            return

        xbmc.log(f"Prehrávam film: {movie_info.get('title', 'Neznámy film')}", xbmc.LOGDEBUG)

        list_item = create_compatible_list_item(
            movie_info.get("title", "Neznámy film"),
            movie_info.get("overview", ""),
            movie_info.get("year", 0),
            default_thumb
        )
        
        list_item.setPath(play_url)
        xbmcplugin.setResolvedUrl(ADDON_HANDLE, True, list_item)

        save_played_movie(movie_id)

    xbmcplugin.endOfDirectory(ADDON_HANDLE)

def select_stream_serie(episodeId):
    try:
        episodeId = int(episodeId)
    except ValueError:
        xbmc.log(f"episodeId nie je číslo: {episodeId}", xbmc.LOGERROR)
        xbmcgui.Dialog().notification("Chyba", "Neplatné ID epizódy.", xbmcgui.NOTIFICATION_ERROR)
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return

    details = list(get_collection("episode_detail_links").find({"episodeId": episodeId}))
    if not details:
        xbmcgui.Dialog().notification("Žiadne súbory", "Pre túto epizódu nie sú dostupné súbory.", xbmcgui.NOTIFICATION_INFO)
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return

    details.sort(
        key=lambda x: (
            resolution_order.get(x.get("resolution", ""), 0),
            float(x.get("size", "0").replace(" GB", ""))
        ),
        reverse=True
    )

    episode = get_collection("episodes").find_one({"episodeId": episodeId})
    serie_info = get_collection("series").find_one({"serieId": episode.get("serieId")})
    season = get_collection("seasons").find_one({"seasonId": episode.get("seasonId")})
    thumb = episode.get("stillUrl") or serie_info.get("posterUrl", "DefaultAddonVideo.png")

    dialog = xbmcgui.Dialog()
    items = []

    for file in details:
        audio = file.get("audio", ["Neznámy"])
        resolution = file.get("resolution", "?")
        size = file.get("size", "?")
        bitrate = file.get("bitrate", "?")
        codec = file.get("videoCodec", "?")
        name = file.get("name", "N/A")

        label = f"[B]{resolution}[/B] • [COLOR FF00FF00]{size}[/COLOR] • [COLOR FFFF00FF]{name}[/COLOR]"
        li = xbmcgui.ListItem(label=label)
        li.setLabel2(f"[COLOR FFFFCC00]{', '.join(audio)}[/COLOR] • {bitrate} • {codec}")
        li.setArt({'thumb': thumb})
        items.append(li)

    index = dialog.select("Vyberte stream: [COLOR FFFFCC00]Rozlišenie • Veľkosť[/COLOR]", items, useDetails=True)

    if index < 0:
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return

    selected_ident = details[index].get("ident")
    play_url = ws.get_stream_url(ident=selected_ident, mongo_collection=get_collection("episode_detail_links"))

    error_messages = {
        "deleted": ("Vymazané", f"Záznam {selected_ident} bol úspešne vymazaný.", xbmcgui.NOTIFICATION_INFO),
        "unauthorized": ("Prístup odmietnutý", "Nemáš práva na mazanie z databázy.", xbmcgui.NOTIFICATION_ERROR),
        "delete_error": ("Chyba", "Mazanie z databázy zlyhalo.", xbmcgui.NOTIFICATION_ERROR),
        "not_found": ("Nenájdené", "Záznam s ident sa nenašiel.", xbmcgui.NOTIFICATION_INFO),
        "password_protected": ("Súbor je zaheslovaný", "Nie je možné ho prehrať.", xbmcgui.NOTIFICATION_ERROR),
        "temporarily_unavailable": ("Dočasne nedostupné", "Súbor je momentálne nedostupný.", xbmcgui.NOTIFICATION_WARNING),
        "non_public_file": ("Súkromný súbor", "Obsah nie je verejný. Môže ísť o autorsky chránené video.", xbmcgui.NOTIFICATION_ERROR),
        None: ("Stream URL", "Nepodarilo sa získať stream URL.", xbmcgui.NOTIFICATION_ERROR)
    }

    if play_url in error_messages:
        title, message, icon = error_messages[play_url]
        xbmcgui.Dialog().notification(title, message, icon)
        xbmcplugin.endOfDirectory(ADDON_HANDLE, succeeded=False)
        return

    label = episode.get("name", "Epizóda")
    year = serie_info.get("year", 0)
    plot = episode.get("overview", "")
    extra_info = {
        "season": season.get("season_number", 1),
        "episode": episode.get("episode_number", 1),
        "tvshowtitle": serie_info.get("title", ""),
        "genre": serie_info.get("genres", ""),
        "plot": plot
    }

    list_item = create_compatible_list_item(label, plot, year, thumb, extra_info)
    list_item.setPath(play_url)
    xbmcplugin.setResolvedUrl(ADDON_HANDLE, True, list_item)

    save_played_series(serie_info.get("serieId"))
    xbmcplugin.endOfDirectory(ADDON_HANDLE)

# CSFD typy na dnes
def typy_na_dnes_csfd():
    xbmcplugin.setPluginCategory(ADDON_HANDLE, 'Tipy na dnes (ČSFD)')
    xbmcplugin.setContent(ADDON_HANDLE, 'movies')

    def fetch_csfd_tips():
        results = fetch_csfd_tip_titles()
        if not results or (isinstance(results, dict) and results.get("error")):
            return []

        movies = []
        for item in results:
            movie = get_collection("movies").find_one({
                "title": {"$regex": f"^{re.escape(item['title'])}$", "$options": "i"},
                "year": item["year"]
            })
            if movie:
                movies.append(movie)
        return movies

    movies = redis_cache.get_or_cache(
        "csfd_tips_full",
        fetch_csfd_tips,
        ttl=21600  # 6 hodín
    )

    if not movies:
        xbmcgui.Dialog().notification("Chyba", "Nenašli sa žiadne filmy", xbmcgui.NOTIFICATION_WARNING)
    else:
        for movie in movies:
            add_movie_listitem(movie, ADDON_HANDLE)

    xbmcplugin.endOfDirectory(ADDON_HANDLE)

def get_movies_by_initial(initial, length=1):
    """Získaj filmy začínajúce na dané písmeno/znaky"""
    cache_key = f"movies_initial_{initial}_{length}"
    
    return redis_cache.get_or_cache(
        cache_key,
        lambda: list(get_collection("movies").find({
            "title": {"$regex": f'^{re.escape(initial)}', "$options": "i"},
            "status": 1
        }).sort("title", 1)),
        ttl=600 # 10 minut
    )

def get_series_by_initial(initial, length=1):
    """Získaj seriály začínajúce na dané písmeno/znaky"""
    cache_key = f"series_initial_{initial}_{length}"
    
    return redis_cache.get_or_cache(
        cache_key,
        lambda: list(get_collection("series").aggregate([
            {"$match": {
                "title": {"$regex": f'^{re.escape(initial)}', "$options": "i"}
            }},
            {"$lookup": {
                "from": "episodes",
                "localField": "serieId",
                "foreignField": "serieId",
                "as": "episodes"
            }},
            {"$match": {
                "episodes.statusWS": 1
            }},
            {"$sort": {"title": 1}}
        ])),
        ttl=600  # 10 minut
    )

def list_movies_by_name(initial=None, length=1):
    """Zobraziť filmy podľa názvu (písmená, dvojice, trojice, zoznam)"""
    xbmcplugin.setPluginCategory(ADDON_HANDLE, 'Filmy podľa názvu')
    xbmcplugin.setContent(ADDON_HANDLE, 'videos')
    
    # Pridanie návratu do hlavného menu
    url = build_url({'action': 'main_menu'})
    li = xbmcgui.ListItem(label='[B]<< Hlavné menu[/B]')
    li.setArt({'icon': 'DefaultFolderBack.png'})
    xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=url, listitem=li, isFolder=True)
    
    if initial is None:
        # Zobraziť písmená A-Z a 0-9
        letters = [chr(i) for i in range(65, 91)]  # A-Z
        letters.extend([str(i) for i in range(10)])  # 0-9
        
        for letter in letters:
            movies = get_movies_by_initial(letter)
            count = len(movies)
            if count > 0:
                url = build_url({'action': 'list_movies_by_name', 'initial': letter, 'length': 1})
                li = xbmcgui.ListItem(label=f'{letter} ({count})')
                xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=url, listitem=li, isFolder=True)
    
    elif length == 1:
        # Zobraziť dvojice písmen pre zvolené písmeno
        movies = get_movies_by_initial(initial)
        first_two_chars = sorted(list(set(movie['title'][:2].upper() for movie in movies if len(movie['title']) >= 2)))
        
        for two_chars in first_two_chars:
            if two_chars.startswith(initial.upper()):
                count = len([m for m in movies if m['title'].upper().startswith(two_chars)])
                if count > 0:
                    url = build_url({'action': 'list_movies_by_name', 'initial': two_chars, 'length': 2})
                    li = xbmcgui.ListItem(label=f'{two_chars} ({count})')
                    xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=url, listitem=li, isFolder=True)
    
    elif length == 2:
        # Zobraziť trojice písmen pre zvolenú dvojicu
        movies = get_movies_by_initial(initial)
        first_three_chars = sorted(list(set(movie['title'][:3].upper() for movie in movies if len(movie['title']) >= 3)))
        
        for three_chars in first_three_chars:
            if three_chars.startswith(initial.upper()):
                count = len([m for m in movies if m['title'].upper().startswith(three_chars)])
                if count > 0:
                    url = build_url({'action': 'list_movies_by_name', 'initial': three_chars, 'length': 3})
                    li = xbmcgui.ListItem(label=f'{three_chars} ({count})')
                    xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=url, listitem=li, isFolder=True)
    
    else:
        # Zobraziť filmy pre zvolenú trojicu písmen
        movies = get_movies_by_initial(initial, length=3)
        for movie in movies:
            add_movie_listitem(movie, ADDON_HANDLE)
    
    xbmcplugin.endOfDirectory(ADDON_HANDLE)

def list_series_by_name(initial=None, length=1):
    """Zobraziť seriály podľa názvu (písmená, dvojice, trojice, zoznam)"""
    xbmcplugin.setPluginCategory(ADDON_HANDLE, 'Seriály podľa názvu')
    xbmcplugin.setContent(ADDON_HANDLE, 'tvshows')
    
    # Pridanie návratu do hlavného menu
    url = build_url({'action': 'main_menu'})
    li = xbmcgui.ListItem(label='[B]<< Hlavné menu[/B]')
    li.setArt({'icon': 'DefaultFolderBack.png'})
    xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=url, listitem=li, isFolder=True)
    
    if initial is None:
        # Zobraziť písmená A-Z a 0-9
        letters = [chr(i) for i in range(65, 91)]  # A-Z
        letters.extend([str(i) for i in range(10)])  # 0-9
        
        for letter in letters:
            series = get_series_by_initial(letter)
            count = len(series)
            if count > 0:
                url = build_url({'action': 'list_series_by_name', 'initial': letter, 'length': 1})
                li = xbmcgui.ListItem(label=f'{letter} ({count})')
                xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=url, listitem=li, isFolder=True)
    
    elif length == 1:
        # Zobraziť dvojice písmen pre zvolené písmeno
        series = get_series_by_initial(initial)
        first_two_chars = sorted(list(set(s['title'][:2].upper() for s in series if len(s['title']) >= 2)))
        
        for two_chars in first_two_chars:
            if two_chars.startswith(initial.upper()):
                count = len([s for s in series if s['title'].upper().startswith(two_chars)])
                if count > 0:
                    url = build_url({'action': 'list_series_by_name', 'initial': two_chars, 'length': 2})
                    li = xbmcgui.ListItem(label=f'{two_chars} ({count})')
                    xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=url, listitem=li, isFolder=True)
    
    elif length == 2:
        # Zobraziť trojice písmen pre zvolenú dvojicu
        series = get_series_by_initial(initial)
        first_three_chars = sorted(list(set(s['title'][:3].upper() for s in series if len(s['title']) >= 3)))
        
        for three_chars in first_three_chars:
            if three_chars.startswith(initial.upper()):
                count = len([s for s in series if s['title'].upper().startswith(three_chars)])
                if count > 0:
                    url = build_url({'action': 'list_series_by_name', 'initial': three_chars, 'length': 3})
                    li = xbmcgui.ListItem(label=f'{three_chars} ({count})')
                    xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=url, listitem=li, isFolder=True)
    
    else:
        # Zobraziť seriály pre zvolenú trojicu písmen
        series = get_series_by_initial(initial, length=3)
        for s in series:
            add_series_listitem(s, ADDON_HANDLE)
    
    xbmcplugin.endOfDirectory(ADDON_HANDLE)

def show_latest_dubbed_movies():
    xbmcplugin.setPluginCategory(ADDON_HANDLE, 'Novinky dabované')
    xbmcplugin.setContent(ADDON_HANDLE, 'movies')

    def fetch_dubbed_movies():
        dubbed_langs = {"CZE", "CSE", "SLK"}
        movies = list(
            get_collection("movies")
            .find({"status": 1, "release_date": {"$exists": True}})
            .sort("release_date", -1)
            .limit(300)
        )

        filtered = []

        for movie in movies:
            movie_id = movie.get("movieId")
            if not movie_id:
                continue

            try:
                audios = get_collection("movie_detail").find({"fkMovieId": movie_id}).distinct("audio")
                for entry in audios:
                    if isinstance(entry, list):
                        for lang in entry:
                            code = re.split(r'[\(\)]', lang)[0].strip().upper()
                            if code in dubbed_langs:
                                filtered.append(movie)
                                break
                    elif isinstance(entry, str):
                        code = re.split(r'[\(\)]', entry)[0].strip().upper()
                        if code in dubbed_langs:
                            filtered.append(movie)

                    if movie in filtered:
                        break

                if len(filtered) >= 100:
                    break

            except Exception as e:
                xbmc.log(f"Chyba pri filtrovaní dabovaných filmov: {str(e)}", xbmc.LOGWARNING)

        return filtered[:100]

    # --- CACHE na 10 minút (600 sekúnd)
    movies = redis_cache.get_or_cache("latest_dubbed_movies", fetch_dubbed_movies, ttl=600)

    for movie in movies:
        add_movie_listitem(movie, ADDON_HANDLE)

    xbmcplugin.endOfDirectory(ADDON_HANDLE)

def list_top_popular_movies_czsk():
    xbmcplugin.setPluginCategory(ADDON_HANDLE, 'Top 100 populárnych filmov (CZ/SK)')
    xbmcplugin.setContent(ADDON_HANDLE, 'movies')

    def fetch_tmdb_popular_ids():

        popular_ids = []
        api_key = addon.getSetting("tmdb_api_key")

        for page in range(1, 6):
            url = f"https://api.themoviedb.org/3/movie/popular?api_key={api_key}&language=cs-CZ&region=CZ&page={page}"
            try:
                with urllib.request.urlopen(url, timeout=10) as response:
                    data = json.loads(response.read().decode('utf-8'))
                    for item in data.get("results", []):
                        popular_ids.append(item["id"])
            except Exception as e:
                xbmc.log(f"Chyba pri načítaní populárnych filmov z TMDb: {e}", xbmc.LOGWARNING)

        return popular_ids

    tmdb_ids = redis_cache.get_or_cache("tmdb_popular_czsk_ids", fetch_tmdb_popular_ids, ttl=21600)

    found_movies = []
    for tmdb_id in tmdb_ids:
        movie = get_collection("movies").find_one({"tmdbId": tmdb_id, "status": 1})
        if movie:
            found_movies.append(movie)
        if len(found_movies) >= 100:
            break

    for movie in found_movies:
        add_movie_listitem(movie, ADDON_HANDLE)

    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def list_trending_movies_last_14_days():
    xbmcplugin.setPluginCategory(ADDON_HANDLE, 'Trending filmy (posledných 14 dní)')
    xbmcplugin.setContent(ADDON_HANDLE, 'movies')

    def fetch_trending_ids():

        trending_ids = set()
        api_key = addon.getSetting("tmdb_api_key")

        for period in ['day', 'week']:
            for i in range(1, 3):
                url = f"https://api.themoviedb.org/3/trending/movie/{period}?api_key={api_key}&page={i}"
                try:
                    with urllib.request.urlopen(url, timeout=10) as response:
                        data = json.loads(response.read().decode('utf-8'))
                        for item in data.get("results", []):
                            trending_ids.add(item["id"])
                except Exception as e:
                    xbmc.log(f"Chyba pri načítaní trending filmov ({period}): {e}", xbmc.LOGWARNING)

        return list(trending_ids)

    tmdb_ids = redis_cache.get_or_cache("tmdb_trending_14days", fetch_trending_ids, ttl=21600)

    found_movies = []
    for tmdb_id in tmdb_ids:
        movie = get_collection("movies").find_one({"tmdbId": tmdb_id, "status": 1})
        if movie:
            found_movies.append(movie)
        if len(found_movies) >= 100:
            break

    for movie in found_movies:
        add_movie_listitem(movie, ADDON_HANDLE)

    xbmcplugin.endOfDirectory(ADDON_HANDLE)
    
def list_top_rated_movies_czsk():
    xbmcplugin.setPluginCategory(ADDON_HANDLE, 'Top 100 najlepšie hodnotených filmov (CZ/SK)')
    xbmcplugin.setContent(ADDON_HANDLE, 'movies')

    def fetch_top_rated_ids():

        top_rated_ids = []
        api_key = addon.getSetting("tmdb_api_key")

        for page in range(1, 6):  # 5 strán po 20 = 100 filmov
            url = f"https://api.themoviedb.org/3/movie/top_rated?api_key={api_key}&language=cs-CZ&region=CZ&page={page}"
            try:
                with request.urlopen(url, timeout=10) as response:
                    data = json.loads(response.read().decode('utf-8'))
                    for item in data.get("results", []):
                        top_rated_ids.append(item["id"])
            except Exception as e:
                xbmc.log(f"Chyba pri načítaní top rated filmov z TMDb: {e}", xbmc.LOGWARNING)

        return top_rated_ids

    tmdb_ids = redis_cache.get_or_cache("tmdb_top_rated_czsk_ids", fetch_top_rated_ids, ttl=21600)

    found_movies = []
    for tmdb_id in tmdb_ids:
        movie = get_collection("movies").find_one({"tmdbId": tmdb_id, "status": 1})
        if movie:
            found_movies.append(movie)
        if len(found_movies) >= 100:
            break

    for movie in found_movies:
        add_movie_listitem(movie, ADDON_HANDLE)

    xbmcplugin.endOfDirectory(ADDON_HANDLE)

params = dict(parse_qsl(sys.argv[2][1:]))
action = params.get('action')

# Router - Main control flow
if action == 'select_stream':
    select_stream(params.get('movieId'))
elif action == 'select_stream_serie':
    select_stream_serie(params.get('episodeId'))
elif action == 'recent_searches':
    list_recent_searches()
elif action == 'recently_played':
    list_played_movies()
elif action == 'recently_played_series':
    list_played_series()
elif action == 'list_movies':
    page = int(params.get('page', '1'))
    query = params.get('query')
    show_movies(query=query, page=page)
elif action == 'show_movies':
    page = int(params.get('page', '1'))
    query = params.get('query')
    show_movies(query=query, page=page)
elif action == 'clear_search_history':
    if xbmcgui.Dialog().yesno("Potvrdenie", "Naozaj chceš vymazať históriu hľadania?"):
        clear_search_history()
elif action == 'clear_played_history':
    if xbmcgui.Dialog().yesno("Potvrdenie", "Naozaj chceš vymazať naposledy sledované?"):
        clear_played_history()
elif action == 'list_series':
    page = int(params.get('page', '1'))
    list_series(page=page)
elif action == 'list_seasons':
    list_seasons(params.get('serieId'))
elif action == 'list_episodes':
    list_episodes(params.get('serieId'), params.get('seasonId'))
elif action == 'search':
    if 'query' in params:
        search_text = params['query']
        page = int(params.get('page', '1'))
        save_search_term(search_text)
        process_tmdb_search_results(search_text, page)
    else:
        search_movies()
elif action == 'search_results':
    if 'query' in params:
        search_text = params['query']
        page = int(params.get('page', '1'))
        process_tmdb_search_results(search_text, page)
elif action == 'show_latest_movies':
    page = int(params.get('page', '1'))
    show_latest_movies(page=page)
elif action == 'show_latest_added_movies':
    page = int(params.get('page', '1'))
    show_latest_added_movies(page=page)
elif action == 'list_latest_series':
    list_latest_series()
elif action == 'list_latest_added_series':
    page = int(params.get('page', '1'))
    list_latest_added_series(page=page)
elif action == 'typy_na_dnes_csfd':
    typy_na_dnes_csfd()
elif action == 'list_movies_by_name':
    list_movies_by_name(params.get('initial'), int(params.get('length', 1)))
elif action == 'list_series_by_name':
    list_series_by_name(params.get('initial'), int(params.get('length', 1)))
elif action == 'show_latest_dubbed_movies':
    show_latest_dubbed_movies()
elif action == 'list_top_popular_movies_czsk':
    list_top_popular_movies_czsk()
elif action == 'list_trending_movies_last_14_days':
    list_trending_movies_last_14_days()
elif action == 'list_top_rated_movies_czsk':
    list_top_rated_movies_czsk()
else:
    # Initial login and main menu
    main_menu()