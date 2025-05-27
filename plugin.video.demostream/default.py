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

# Kodi používa len základné knižnice, xml parsovanie urobíme bez xmltodict
import xml.etree.ElementTree as ET

addon_dir = os.path.dirname(__file__)
sys.path.append(os.path.join(addon_dir, 'resources', 'lib', 'python'))

from bson import ObjectId
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure
from urllib.parse import parse_qsl
from urllib import request, parse
from tmdb.searchTMDBMulti import search_movie_tmdb

try:
    from xbmc import translatePath
except ImportError:
    from xbmcvfs import translatePath

    # MongoDB Connection Manager
class MongoDBConnection:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialize()
        return cls._instance

    def _initialize(self):
        #uri = f"mongodb+srv://{mongodb_user}:{mongodb_pass}@{mongodb_host}/sc3?authSource=sc3"
        uri = f"mongodb://{mongodb_user}:{mongodb_pass}@{mongodb_host}/{mongodb_db}?authSource={mongodb_db}"
        self.client = MongoClient(uri, maxPoolSize=50, connectTimeoutMS=5000, socketTimeoutMS=30000)
        self.db = self.client[mongodb_db]

    def get_db(self):
        try:
            self.client.admin.command('ping')
            return self.db
        except ConnectionFailure:
            self._initialize()
            return self.db

    def close(self):
        if self.client:
            self.client.close()
        self._instance = None

# With this function:
def get_collection(collection_name):
    """Get a MongoDB collection using the connection manager"""
    return db_connection.get_db()[collection_name]

ADDON_HANDLE = int(sys.argv[1])
BASE_URL = sys.argv[0]
ARGS = urllib.parse.parse_qs(sys.argv[2][1:])

# Priečinky pre súbory
addon = xbmcaddon.Addon()
profile_dir = translatePath(addon.getAddonInfo('profile'))
STORAGE_DIR = os.path.join(profile_dir, 'store')
SEARCH_HISTORY_FILE = os.path.join(STORAGE_DIR, 'search_terms.json')
MOVIE_HISTORY_FILE = os.path.join(STORAGE_DIR, 'movie_history.json')
SERIE_HISTORY_FILE = os.path.join(STORAGE_DIR, 'serie_history.json')

# mongodb settings
mongodb_user = addon.getSetting("mongodb_user")
mongodb_pass = addon.getSetting("mongodb_pass")
mongodb_host = addon.getSetting("mongodb_host")
mongodb_db = addon.getSetting("mongodb_db")
# webshare settings
webshare_user = addon.getSetting("webshare_user")
webshare_pass = addon.getSetting("webshare_pass")
webshare_url = addon.getSetting("webshare_url")
REALM = ":Webshare:"
webshare_token = None

# Initialize MongoDB connection
db_connection = MongoDBConnection()
db = db_connection.get_db()

# Initialize collections
def get_collection(collection_name):
    return db[collection_name]

# Log adresárov a vytvorenie priečinka
os.makedirs(STORAGE_DIR, exist_ok=True)

PER_PAGE = 20  # počet poloziek na jednu stranu
HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "text/xml"
}

#-------- XML parsovanie --------
def parse_xml_response(text):
    try:
        root = ET.fromstring(text)
        return {child.tag: child.text for child in root}
    except Exception as e:
        xbmc.log(f"❌ Chyba pri XML parsovaní: {e}", xbmc.LOGERROR)
        return {}

#-------- Webshare login --------
def webshare_login():
    global webshare_token
    try:
        print("🔐 Získavam salt z Webshare...")

        salt_payload = urllib.parse.urlencode({"username_or_email": webshare_user}).encode("utf-8")
        salt_req = urllib.request.Request(f"{webshare_url}salt/", data=salt_payload, headers=HEADERS)
        with urllib.request.urlopen(salt_req) as response:
            salt_response = response.read().decode("utf-8")
        salt_dict = parse_xml_response(salt_response)
        salt = salt_dict.get("salt")
        if not salt:
            xbmc.log("❌ Nepodarilo sa získať salt.", xbmc.LOGERROR)
            return

        xbmc.log(f"✅ Salt úspešne získaný. Salt: {salt}", xbmc.LOGINFO)

        encrypted_pass = hashlib.sha1(md5crypt(webshare_pass.encode('utf-8'), salt.encode('utf-8')).encode('utf-8')).hexdigest()
        pass_digest = hashlib.md5((webshare_user + REALM + webshare_pass).encode('utf-8')).hexdigest()

        login_payload = {
            "username_or_email": webshare_user,
            "password": encrypted_pass,
            "digest": pass_digest
        }

        xbmc.log("🔐 Prihlasujem sa do Webshare...", xbmc.LOGINFO)
        login_data = urllib.parse.urlencode(login_payload).encode("utf-8")
        login_req = urllib.request.Request(f"{webshare_url}login/", data=login_data, headers=HEADERS)

        with urllib.request.urlopen(login_req) as response:
            login_response = response.read().decode("utf-8")
        login_dict = parse_xml_response(login_response)
        if login_dict.get("status") != "OK" or not login_dict.get("token"):
            xbmc.log(f"❌ Prihlásenie zlyhalo: {login_response}", xbmc.LOGERROR)
            xbmcgui.Dialog().notification("Webshare", "Prihlásenie zlyhalo.", xbmcgui.NOTIFICATION_ERROR)
            webshare_token = None
            return

        webshare_token = login_dict.get("token")
        if webshare_token:
            # Store token with 10-hour validity
            # Set expiration to 10 hours from now
            expiry_time = datetime.now() + timedelta(hours=10)
            addon.setSetting("token_expiry", expiry_time.isoformat())
            addon.setSetting("webshare_token", webshare_token)
            xbmc.log(f"✅ Token valid until: {expiry_time}", xbmc.LOGINFO)
        return True

    except Exception as e:
        xbmc.log(f"❌ Výnimka pri prihlasovaní do Webshare: {e}", xbmc.LOGERROR)

#-------- Token validácia --------
def is_token_valid():
    """Check if stored token is still valid"""
    try:
        token = addon.getSetting("webshare_token")
        expiry_str = addon.getSetting("token_expiry")

        if not token or not expiry_str:
            return False

        expiry_time = datetime.fromisoformat(expiry_str)
        return datetime.now() < expiry_time
    except Exception as e:
        xbmc.log(f"Token validation error: {str(e)}", xbmc.LOGERROR)
        return False

#-------- Token získanie --------
def get_webshare_token():
    """Get valid token without unnecessary refresh"""
    global webshare_token

    # Return valid in-memory token if available
    if webshare_token and is_token_valid():
        return webshare_token

    # Check stored token
    stored_token = addon.getSetting("webshare_token")
    if stored_token and is_token_valid():
        webshare_token = stored_token
        return webshare_token

    # Only login if token is missing or expired
    if webshare_login():
        return webshare_token

    return None

#-------- Webshare stream URL --------
def get_webshare_stream_url(ident, token):
    url = webshare_url + "file_link/"
    payload = {"ident": ident, "wst": token}
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    data = urllib.parse.urlencode(payload).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=headers)

    try:
        with urllib.request.urlopen(req) as response:
            response_text = response.read().decode("utf-8")
            link_dict = parse_xml_response(response_text)
            if link_dict.get("status") != "OK":
                xbmc.log(f"❌ Webshare nevrátil OK pre ident {ident}", xbmc.LOGERROR)
                return None
            xbmc.log(f"✅ Webshare stream URL pre ident {ident}: bol úspešne získaný", xbmc.LOGINFO)
            return link_dict.get("link")
    except Exception as e:
        xbmc.log(f"❌ Chyba pri načítaní odkazu pre ident {ident}: {e}", xbmc.LOGERROR)
        return None

def format_time(seconds):
    mins, secs = divmod(int(seconds), 60)
    return f"{mins}:{secs:02d}"

#-------- Pridanie položky do zoznamu pre movies --------
def add_movie_listitem(movie, addon_handle):
    title = movie.get("title")
    year = movie.get("year")
    movie_id = movie.get("movieId")
    poster_url = movie.get("posterUrl")
    overview = movie.get("overview", "Žiadny popis k dispozícii.")

    url = build_url({'action': 'select_stream', 'movieId': movie_id})

    # Získanie audio jazykov
    languages_str = ""
    try:
        audio_languages = get_collection("movie_detail").find({"fkMovieId": movie_id}).distinct("audio")
        processed_languages = set()

        for lang_entry in audio_languages:
            if isinstance(lang_entry, list):
                for lang in lang_entry:
                    if lang:
                        lang_code = re.split(r'[\(\)]', lang)[0].strip().upper()
                        if lang_code == "UND":
                            lang_code = "CZE"
                        processed_languages.add(lang_code)
            elif isinstance(lang_entry, str):
                lang_code = re.split(r'[\(\)]', lang_entry)[0].strip().upper()
                if lang_code == "UND":
                    lang_code = "CZE"
                processed_languages.add(lang_code)

        if processed_languages:
            languages_str = f" [COLOR deepskyblue]{', '.join(sorted(processed_languages))}[/COLOR]"

    except Exception as e:
        xbmc.log(f"Chyba pri získavaní jazykov pre movieId {movie_id}: {str(e)}", xbmc.LOGWARNING)

    # Formátovaný label
    label = f"[B]{title}[/B]"
    if year:
        label += f" [COLOR grey]({year})[/COLOR]"
    label += languages_str

    li = xbmcgui.ListItem(label=label)
    li.setInfo('video', {'title': title, 'year': year, 'plot': overview})
    li.setArt({'thumb': poster_url} if poster_url else {'thumb': 'DefaultVideo.png'})
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
        ('Naposledy pridané filmy', 'show_latest_added_movies', 'DefaultRecentlyAddedMovies.png'),
        ('Naposledy pridané seriály', 'list_latest_added_series', 'DefaultRecentlyAddedEpisodes.png'),
        ('Najnovšie filmy', 'show_latest_movies', 'DefaultRecentlyAddedMovies.png'),
        ('Najnovšie seriály', 'list_latest_series', 'DefaultRecentlyAddedEpisodes.png'),
        ('Filmy', 'show_movies', 'DefaultVideo.png'),
        ('Seriály', 'list_series', 'DefaultTVShows.png'),
        ('Tipy na dnes (ČSFD)', 'typy_na_dnes_csfd', 'DefaultTVShows.png'),
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
        movies = get_collection("movies").find(mongo_query).sort("movieId", -1).skip(skip_count).limit(PER_PAGE)

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
                            lang_code = "CZE"
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
        url = build_url({'action': 'search', 'query': term})
        li = xbmcgui.ListItem(label=f"{term}")
        xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=url, listitem=li, isFolder=True)

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
        all_seasons = list(get_collection("seasons").find({"serieId": serieId}).sort("season_number", 1))

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

        episodes = get_collection("episodes").find({
            "serieId": serieId,
            "seasonId": seasonId,
            "statusWS": 1
        }).sort("episode_number", 1)

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
    global webshare_token

    try:
        movie_id = int(movie_id)
    except ValueError:
        xbmc.log(f"movie_id nie je číslo: {movie_id}", xbmc.LOGERROR)
        xbmcgui.Dialog().notification("Chyba", "Neplatné ID filmu.", xbmcgui.NOTIFICATION_ERROR, 3000)
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return

    # Get or refresh token with 10-hour validity
    token = get_webshare_token()
    if not token:
        xbmcgui.Dialog().notification("Chyba", "Webshare prihlásenie zlyhalo.", xbmcgui.NOTIFICATION_ERROR, 3000)
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return

    details = list(get_collection("movie_detail").find({"fkMovieId": movie_id}))
    if not details:
        xbmcgui.Dialog().notification("Žiadne súbory", "Pre tento film nie sú dostupné žiadne súbory.", xbmcgui.NOTIFICATION_INFO, 3000)
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return

    details.sort(key=lambda x: float(x["size"].replace(" GB", "")), reverse=True)

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
        play_url = get_webshare_stream_url(selected_ident, token)
        if play_url:
            xbmc.log(f"Prehrávam film: {movie_info.get('title', 'Neznámy film')}", xbmc.LOGDEBUG)

            list_item = xbmcgui.ListItem(label=movie_info.get('title', 'Neznámy film'))
            list_item.setPath(play_url)
            list_item.setProperty('IsPlayable', 'true')

            # Modern Kodi: use InfoTagVideo instead of deprecated setInfo
            info_tag = list_item.getVideoInfoTag()
            info_tag.setTitle(movie_info.get("title", "Neznámy film"))
            info_tag.setPlot(movie_info.get("overview", ""))
            try:
                info_tag.setYear(int(movie_info.get("year", 0)))
            except:
                xbmc.log(f"Neplatný rok: {movie_info.get('year', 'N/A')}", xbmc.LOGERROR)

            # Optional but helpful for movie handling
            info_tag.setMediaType("movie")

            # Set art
            li.setArt({
                'thumb': default_thumb,
                'poster': default_thumb,
                'fanart': default_thumb
            })

            # Now resolve the URL for Kodi to handle playback
            xbmcplugin.setResolvedUrl(ADDON_HANDLE, True, list_item)

            save_played_movie(movie_id)
        else:
            xbmcgui.Dialog().notification("Chyba", "Nepodarilo sa získať URL pre prehrávanie.", xbmcgui.NOTIFICATION_ERROR, 3000)

    xbmcplugin.endOfDirectory(ADDON_HANDLE)

def select_stream_serie(episodeId):
    global webshare_token

    try:
        episodeId = int(episodeId)
    except ValueError:
        xbmc.log(f"episodeId nie je číslo: {episodeId}", xbmc.LOGERROR)
        xbmcgui.Dialog().notification("Chyba", "Neplatné ID epizódy.", xbmcgui.NOTIFICATION_ERROR, 3000)
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return

    # Get or refresh token with 10-hour validity
    token = get_webshare_token()
    if not token:
        xbmcgui.Dialog().notification("Chyba", "Webshare prihlásenie zlyhalo.", xbmcgui.NOTIFICATION_ERROR, 3000)
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return

    # Get all available files for this episode
    details = list(get_collection("episode_detail_links").find({"episodeId": episodeId}))
    # Check if details are empty
    if not details:
        xbmcgui.Dialog().notification("Žiadne súbory", "Pre túto epizódu nie sú dostupné súbory.", xbmcgui.NOTIFICATION_INFO, 3000)
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return

    details.sort(key=lambda x: float(x["size"].replace(" GB", "")), reverse=True)

    # Get serie for thumbnail
    episode = get_collection("episodes").find_one({"episodeId": episodeId})
    serieId = episode.get("serieId")
    serie_info = get_collection("series").find_one({"serieId": serieId})
    default_thumb = serie_info.get('posterUrl', 'DefaultAddonVideo.png')
    defualt_thumb_ep = episode.get("stillUrl", serie_info.get('posterUrl', 'DefaultAddonVideo.png'))
    season = get_collection("seasons").find_one({"seasonId": episode.get("seasonId")})

    dialog = xbmcgui.Dialog()
    items = []

    for file in details:
        audio_streams = file.get("audio", ["Neznámy"])
        resolution = file.get("resolution", "?")
        size = file.get("size", "?")
        bitrate = file.get("bitrate", "?")
        video_codec = file.get("videoCodec", "?")
        filename = file.get("name", "N/A")

        # Create scrollable filename with max 30 chars viewable at once
        scrollable_filename = f"[COLOR FFFF00FF]{filename}[/COLOR]" if filename else ""

        # Create the ListItem with filename
        li = xbmcgui.ListItem(label=f"[B]{resolution}[/B]  •  [COLOR FF00FF00]{size}[/COLOR]  •  {scrollable_filename}")

        # Second line (info line)
        li.setLabel2(f"[COLOR FFFFCC00]{', '.join(audio_streams)}[/COLOR] • {bitrate} • {video_codec}")

        # Set thumbnail (use actual thumbnails if available)
        li.setArt({'thumb': defualt_thumb_ep, 'icon': 'DefaultAddonVideo.png'})

        # Convert size to MB for internal use (handles "3.01 GB" format)
        size_mb = 0
        try:
            size_num = float(size.split()[0])
            if "GB" in size:
                size_mb = int(size_num * 1024)
            elif "MB" in size:
                size_mb = int(size_num)
        except (ValueError, IndexError):
            pass

        # Set additional info for skins that support it
        li.setInfo('video', {
            'size': size_mb,
            'video_codec': video_codec,
            'audio_codec': ', '.join(audio_streams)
        })

        items.append(li)

    # Use select dialog with ListItems for better visual presentation
    index = dialog.select(
        "Vyberte stream: [COLOR FFFFCC00]Rozlišenie • Veľkosť[/COLOR]",
        items,
        useDetails=True  # This enables the two-line display with thumbnails
    )

    if index >= 0:
        selected_ident = details[index].get("ident")
        play_url = get_webshare_stream_url(selected_ident, token)
        if play_url:
            li = xbmcgui.ListItem(label=episode.get("name", "Neznámy epizódy"), path=play_url)
            li.setProperty("IsPlayable", "true")

            # Add basic metadata
            li.setInfo("video", {
                "title": episode.get("name", ""),
                "season": season.get("season_number", 1),
                "episode": episode.get("episode_number", 1),
                "tvshowtitle": serie_info.get("title", ""),
                "genre": serie_info.get("genres", ""),
                "year": serie_info.get("year", ""),
                "plot": episode.get("overview", ""),
                "playcount": 0  # Kodi manages this automatically isf omitted
            })

            # Optional thumbnail/poster
            li.setArt({
                "thumb": defualt_thumb_ep,
                "poster": defualt_thumb_ep,
                "icon": "DefaultVideo.png"
            })

            xbmcplugin.setResolvedUrl(ADDON_HANDLE, True, listitem=li)

            save_played_series(serieId)

        else:
            xbmcgui.Dialog().notification("Chyba", "Nepodarilo sa získať URL pre prehrávanie.", xbmcgui.NOTIFICATION_ERROR, 3000)

    xbmcplugin.endOfDirectory(ADDON_HANDLE)

# CSFD typy na dnes
def typy_na_dnes_csfd():
    xbmcplugin.setPluginCategory(ADDON_HANDLE, 'Tipy na dnes (ČSFD)')
    xbmcplugin.setContent(ADDON_HANDLE, 'movies')

    url = "https://www.csfd.cz/televize/"
    headers = {'User-Agent': 'Mozilla/5.0'}
    req = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            html = response.read().decode("utf-8")
    except Exception as e:
        xbmcgui.Dialog().ok("Chyba načítania", f"Nepodarilo sa načítať stránku ČSFD: {e}")
        return

    # Regulačný výraz na získanie názvu filmu a roka
    pattern = r'<a href="/film/\d+-[^/]+/" class="film-title-name">([^<]+)</a>\s*<span class="film-title-info"><span class="info">\((\d{4})\)</span></span>'
    matches = re.findall(pattern, html, re.DOTALL)

    if not matches:
        xbmcgui.Dialog().ok("Žiadne tipy", "Neboli nájdené žiadne tipy na dnes.")
        return

    for csfd_title, year in matches:
        title_clean = csfd_title.strip()
        xbmc.log(f"Hľadám film: {title_clean} ({year})", xbmc.LOGINFO)

        query = {
            "title": {"$regex": f"^{re.escape(title_clean)}$", "$options": "i"}
        }

        if year and isinstance(year, str) and year.isdigit():
            query["year"] = year  # pridáš ako string

        movie = get_collection("movies").find_one(query)

        if not movie:
            continue

        add_movie_listitem(movie, ADDON_HANDLE)

    xbmcplugin.endOfDirectory(ADDON_HANDLE)

params = dict(parse_qsl(sys.argv[2][1:]))
action = params.get('action')

# Router - Main control flow
if action == 'select_stream':
    token = get_webshare_token()  # Will only refresh if expired
    if token:
        select_stream(params.get('movieId'))
    else:
        xbmcgui.Dialog().notification("Error", "Login failed", xbmcgui.NOTIFICATION_ERROR)
elif action == 'select_stream_serie':
    token = get_webshare_token()  # Will only refresh if expired
    if token:
        select_stream_serie(params.get('episodeId'))
    else:
        xbmcgui.Dialog().notification("Error", "Login failed", xbmcgui.NOTIFICATION_ERROR)
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
else:
    # Initial login and main menu
    webshare_login()
    main_menu()