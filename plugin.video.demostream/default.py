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
from concurrent.futures import ThreadPoolExecutor, as_completed
import xml.etree.ElementTree as ET

addon_dir = os.path.dirname(__file__)
sys.path.append(os.path.join(addon_dir, 'resources', 'lib', 'python'))

from bson import ObjectId
from urllib.parse import parse_qsl
from urllib import request, parse
from tmdb.searchTMDBMulti import search_movie_tmdb
from resources.lib.webshare_client import WebshareClient
from resources.lib.csfd_client import fetch_csfd_tip_titles
from redis_cache import redis_cache
from resources.lib import mongo_api

try:
    from xbmc import translatePath
except ImportError:
    from xbmcvfs import translatePath

# Načítanie nastavení doplnku
addon = xbmcaddon.Addon()
webshare_user = addon.getSetting("webshare_user")
webshare_pass = addon.getSetting("webshare_pass")

# Skontroluj chýbajúce
missing_settings = []
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
WATCH_LATER_FILE = os.path.join(STORAGE_DIR, 'watch_later.json')
PER_PAGE = addon.getSettingInt("per_page") or 20

# Priorita rozlíšení
resolution_order = {
    "4K": 5,
    "FHD": 4,
    "HD": 3,
    "SD": 2,
    "480p": 1
}

# Pomocná funkcia na získanie kolekcie - už nie je potrebná
# def get_collection(collection_name):
#     return db[collection_name]

def get_ws():
    global ws
    if 'ws' not in globals():
        from resources.lib.webshare_client import WebshareClient
        ws = WebshareClient()
    return ws

# Načítanie žánrov z databázy
_genre_dict_cache = None
def get_genre_dict():
    global _genre_dict_cache
    if _genre_dict_cache is None:
        _genre_dict_cache = {
            str(g["id"]): g["name"]
            for g in mongo_api.get_items("movie_genres")
        }
    return _genre_dict_cache

# Vytvorenie úložného priečinka
os.makedirs(STORAGE_DIR, exist_ok=True)

def format_time(seconds):
    mins, secs = divmod(int(seconds), 60)
    return f"{mins}:{secs:02d}"

def create_list_item_legacy(title, plot, year, thumb_url, extra_info={}):
    li = xbmcgui.ListItem(label=title)
    info = {
        "title": title or "",
        "plot": plot or "",
        "year": year or 0,
        "mediatype": "video"
    }
    info.update(extra_info)
    li.setInfo("video", info)  # fallback pre staršie Kodi
    li.setArt({'thumb': thumb_url, 'poster': thumb_url, 'fanart': thumb_url})
    li.setProperty("IsPlayable", "true")
    return li

def create_list_item_modern(title, plot, year, thumb_url, extra_info={}):
    li = xbmcgui.ListItem(label=title)
    info = {
        "title": title or "",
        "plot": plot or "",
        "year": year or 0,
        "mediatype": "video"
    }
    info.update(extra_info)
    set_video_info_tag(li, info)
    li.setArt({'thumb': thumb_url, 'poster': thumb_url, 'fanart': thumb_url})
    li.setProperty("IsPlayable", "true")
    return li

def create_compatible_list_item(title, plot, year, thumb_url, extra_info=None):
    if extra_info is None:
        extra_info = {}
    
    # Create base info dict
    info = {
        'title': str(title) if title is not None else "",
        'plot': str(plot) if plot is not None else "",
        'mediatype': extra_info.get('mediatype', 'video')
    }
    
    # Handle year - ensure it's either int or None
    try:
        info['year'] = int(year) if year is not None and str(year).strip() else None
    except (ValueError, TypeError):
        info['year'] = None
    
    # Merge with extra info
    info.update(extra_info)
    
    # Create list item
    li = xbmcgui.ListItem(label=str(title) if title is not None else xbmcgui.ListItem())
    
    try:
        kodi_major = int(xbmc.getInfoLabel("System.BuildVersion").split(".")[0])
    except:
        kodi_major = 18  # fallback

    if kodi_major >= 20:
        set_video_info_tag(li, info)
    else:
        # For older Kodi versions, use setInfo
        li.setInfo('video', info)
    
    # Set art
    art = {'thumb': thumb_url, 'poster': thumb_url, 'fanart': thumb_url}
    li.setArt(art)
    
    return li

def create_listitem_with_context(label, info, art, url, is_folder, context_items=None):
    li = create_compatible_list_item(
        title=info.get('title', ''),
        plot=info.get('plot', ''),
        year=info.get('year', 0),
        thumb_url=art.get('thumb', ''),
        extra_info=info
    )
    li.setLabel(label)
    li.setArt(art)
    if context_items:
        li.addContextMenuItems(context_items)
    xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=url, listitem=li, isFolder=is_folder)

def load_watch_later():
    if not os.path.exists(WATCH_LATER_FILE):
        return []
    with open(WATCH_LATER_FILE, 'r', encoding='utf-8') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []

def save_watch_later(items):
    with open(WATCH_LATER_FILE, 'w', encoding='utf-8') as f:
        json.dump(items, f, indent=2)

def add_to_watch_later(item_type, item_id):
    items = load_watch_later()
    if any(i for i in items if i['type'] == item_type and i['id'] == item_id):
        return  # already exists
    items.append({'type': item_type, 'id': item_id})
    save_watch_later(items)
    xbmcgui.Dialog().notification("Pridané", "Pridané do 'Pozrieť si neskôr'", xbmcgui.NOTIFICATION_INFO)

def remove_from_watch_later(item_type, item_id):
    items = load_watch_later()
    items = [i for i in items if not (i['type'] == item_type and i['id'] == item_id)]
    save_watch_later(items)
    xbmcgui.Dialog().notification("Odstránené", "Odstránené zo zoznamu", xbmcgui.NOTIFICATION_INFO)
    xbmc.executebuiltin("Container.Refresh")

#-------- Pridanie položky do zoznamu pre movies --------
def add_movie_listitem(movie, addon_handle, context_items=None):
    title = movie.get("title")
    year = movie.get("year")
    movie_id = movie.get("movieId")
    poster_url = movie.get("posterUrl")
    overview = movie.get("overview", "Žiadny popis k dispozícii.")  # Získanie popisu
    vote = movie.get("vote_average")
    release_date = movie.get("release_date")
    stream_languages = movie.get("stream_audio")

    url = build_url({'action': 'select_stream', 'movieId': movie_id})

    # Formátovanie labelu
    label = f"[B]{title}[/B]"
    if year:
        label += f" [COLOR grey]({year})[/COLOR]"
    if vote:
        label += f" [COLOR gold]{vote:.1f}★[/COLOR]"
    if stream_languages:
        label += f" [COLOR deepskyblue]{stream_languages}[/COLOR]"

    # Vytvorenie extra_info s metadátami
    extra_info = {
        "plot": overview,  # Priamy predaj popisu
        "rating": float(vote) if vote else None,
        "premiered": release_date,
        "genre": ", ".join([get_genre_dict().get(str(gid), "") for gid in movie.get("genres", [])])
    }

    # Vytvorenie kompatibilnej položky
    li = create_compatible_list_item(
        title=title,
        plot=overview,  # Popis sa predáva aj tu
        year=year,
        thumb_url=poster_url or 'DefaultVideo.png',
        extra_info=extra_info  # Všetky metadáta
    )
    li.setLabel(label)
    li.setProperty('movie_id', str(movie_id))
    li.setProperty('IsPlayable', 'true')

    # Pridanie kontextového menu
    if context_items is None:
        context_items = [(
            "Pozrieť si neskôr ...",
            f'RunPlugin({build_url({"action": "add_watch_later", "type": "movie", "id": movie_id})})'
        )]
    if context_items:
        li.addContextMenuItems(context_items)

    xbmcplugin.addDirectoryItem(handle=addon_handle, url=url, listitem=li, isFolder=False)

#-------- Pridanie položky do zoznamu pre seriály --------
def add_series_listitem(series, addon_handle, context_items=None):
    title = series.get("title", "Neznámy názov")
    serie_id = series.get("serieId")
    poster_url = series.get("posterUrl")
    overview = series.get("overview", "Žiadny popis k dispozícii.")
    
    # Handle year information
    first_air_date = series.get("first_air_date", "")
    year = int(first_air_date[:4]) if first_air_date and first_air_date[:4].isdigit() else None
    
    url = build_url({'action': 'list_seasons', 'serieId': serie_id})
    
    # Format label
    label = f"[B]{title}[/B]"
    if year:
        label += f" [COLOR grey]({year})[/COLOR]"
    
    # Prepare extra info
    extra_info = {
        'mediatype': 'tvshow',
        'tvshowtitle': title,
        'year': year,
        'premiered': first_air_date,
        'genre': ", ".join([get_genre_dict().get(str(gid), "") for gid in series.get("genres", [])])
    }

    li = create_compatible_list_item(
        title=title,
        plot=overview,
        year=year,
        thumb_url=poster_url or 'DefaultTVShows.png',
        extra_info=extra_info
    )
    li.setLabel(label)
    
    if context_items is None:
        context_items = [(
            "Pozrieť si neskôr ...",
            f'RunPlugin({build_url({"action": "add_watch_later", "type": "serie", "id": serie_id})})'
        )]
    if context_items:
        li.addContextMenuItems(context_items)
        
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
        ('Naposledy hľadané', 'recent_searches', 'DefaultFolder.png'),
        ('Naposledy sledované filmy', 'recently_played', 'DefaultPlaylist.png'),
        ('Naposledy sledované seriály', 'recently_played_series', 'DefaultPlaylist.png'),
        ('Najnovšie filmy', 'show_latest_movies', 'DefaultRecentlyAddedMovies.png'),
        ('Novinky dabované (dátum vydania)', 'show_latest_dubbed_movies', 'DefaultRecentlyAddedMovies.png'),
        ('Filmy s CZ/SK dabingom (najnovší stream)', 'show_movies_with_cz_audio', 'DefaultRecentlyAddedMovies.png'),
        ('Najnovšie seriály', 'list_latest_series', 'DefaultRecentlyAddedEpisodes.png'),
        ('Tipy na dnes (ČSFD)', 'typy_na_dnes_csfd', 'DefaultTVShows.png'),
        ('Trending filmy (posledných 14 dní) TMDB', 'list_trending_movies_last_14_days', 'DefaultMovies.png'),
        ('Top 100 populárnych filmov (CZ/SK) TMDB', 'list_top_popular_movies_czsk', 'DefaultMovies.png'),
        ('Top 100 najlepšie hodnotených filmov (CZ/SK) TMDB', 'list_top_rated_movies_czsk', 'DefaultMovies.png'),
        ('Naposledy pridané filmy', 'show_latest_added_movies', 'DefaultRecentlyAddedMovies.png'),
        ('Naposledy pridané seriály', 'list_latest_added_series', 'DefaultRecentlyAddedEpisodes.png'),
        ('Pozrieť si neskôr', 'list_watch_later', 'DefaultFolder.png'),
        ('Filmy podľa názvu (A-Z)', 'list_movies_by_name', 'DefaultVideo.png'),
        ('Seriály podľa názvu (A-Z)', 'list_series_by_name', 'DefaultTVShows.png'),
        ('Vymazať históriu hľadania', 'clear_search_history', 'DefaultVideoDeleted.png'),
        ('Vymazať naposledy sledované zoznamy', 'clear_played_history', 'DefaultVideoDeleted.png'),
        ('Filmy', 'show_movies', 'DefaultVideo.png'),
        ('Seriály', 'list_series', 'DefaultTVShows.png')
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

    # Pridaj navigáciu na začiatok
    url = build_url({'action': 'main_menu'})
    li = xbmcgui.ListItem(label='[B]<< Prejdi na hlavné menu[/B]')
    li.setArt({'icon': 'DefaultFolderBack.png'})
    xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=url, listitem=li, isFolder=True)

    # Nacitaj predchádzajúce výsledky
    tmdb_results = search_movie_tmdb(query)
    if not tmdb_results:
        xbmcgui.Dialog().notification("Info", "Nenašli sa žiadne výsledky.", xbmcgui.NOTIFICATION_INFO, 3000)
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return

    # konverzia tmdbId na int a filtrovanie podľa mediaType
    movie_ids = [int(item['tmdbId']) for item in tmdb_results if item['mediaType'] == 'movie']
    series_ids = [int(item['tmdbId']) for item in tmdb_results if item['mediaType'] == 'tv']
    
    xbmc.log(f"Found {len(movie_ids)} movies and {len(series_ids)} series for query: {query}", xbmc.LOGINFO)
    
    # Hromadné načítanie filmov a seriálov
    movies_found = []
    series_found = []

    if movie_ids:
        movies_found = list(mongo_api.get_items(
            "movies",
            query={
                "tmdbId": {"$in": movie_ids},
                "status": 1
            }
        ))

    if series_ids:
        series_found = list(mongo_api.get_items(
            "series",
            query={
                "tmdbId": {"$in": series_ids}
            }
        ))

    # Spojiť výsledky a zachovať pôvodné poradie
    all_results = []
    id_to_movie = {int(m['tmdbId']): m for m in movies_found}
    id_to_series = {int(s['tmdbId']): s for s in series_found}

    for item in tmdb_results:
        tmdb_id = int(item['tmdbId'])
        if item['mediaType'] == 'movie' and tmdb_id in id_to_movie:
            all_results.append(('movie', id_to_movie[tmdb_id]))
        elif item['mediaType'] == 'tv' and tmdb_id in id_to_series:
            all_results.append(('series', id_to_series[tmdb_id]))

    # Paginácia
    skip_count = (page - 1) * PER_PAGE
    paged_results = all_results[skip_count:skip_count + PER_PAGE]

    # Pridať "Previous page" ak je potrebné
    if page > 1:
        prev_page_url = build_url({'action': 'search_results', 'query': query, 'page': page - 1})
        li = xbmcgui.ListItem(label='[B]< Predošlá strana[/B]')
        li.setArt({'icon': 'DefaultFolderBack.png'})
        xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=prev_page_url, listitem=li, isFolder=True)

    # Zobraziť výsledky
    for item_type, item in paged_results:
        if item_type == 'movie':
            add_movie_listitem(item, ADDON_HANDLE)
        else:
            add_series_listitem(item, ADDON_HANDLE)

    # Pridať "Next page" ak je potrebné
    if len(all_results) > skip_count + PER_PAGE:
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
        
        movies = mongo_api.get_items(
            "movies",
            query=mongo_query,
            sort={"movieId": -1},
            skip=skip_count,
            limit=PER_PAGE
        )

        # Pridaj navigáciu na začiatok
        add_pagination_controls(page, 0, PER_PAGE, query, action='show_movies')

        count = 0
        for movie in movies:
            count += 1
            add_movie_listitem(movie, ADDON_HANDLE)

        # Pridaj navigáciu na koniec, ak treba
        add_pagination_controls(page, count, PER_PAGE, query, action='show_movies')

    except Exception as e:
        xbmc.log(f"Error in show_movies: {str(e)}", xbmc.LOGERROR)
        xbmcgui.Dialog().notification("Chyba", "Problém s pripojením k API", xbmcgui.NOTIFICATION_ERROR)
    finally:
        xbmcplugin.endOfDirectory(ADDON_HANDLE)

# Zoznam filmov podla datumu vydania
def show_latest_movies(page=1):
    xbmcplugin.setPluginCategory(ADDON_HANDLE, 'Najnovšie filmy')
    xbmcplugin.setContent(ADDON_HANDLE, 'movies')

    skip_count = (page - 1) * PER_PAGE

    movies = mongo_api.get_items(
        "movies",
        query={"status": 1},
        sort={"release_date": -1},
        skip=skip_count,
        limit=PER_PAGE
    )

    movies_list = list(movies)

    # Navigácia hore
    add_pagination_controls(page, 0, PER_PAGE, action='show_latest_movies')

    count = 0
    for movie in movies_list:
        count += 1
        # Pridané správne predanie plotu a ďalších metadát
        add_movie_listitem(movie, ADDON_HANDLE)

    # Navigácia dole
    add_pagination_controls(page, count, PER_PAGE, action='show_latest_movies')

    xbmcplugin.endOfDirectory(ADDON_HANDLE)

# Zoznam filmov podla datumu pridania
def show_latest_added_movies(page=1):
    xbmcplugin.setPluginCategory(ADDON_HANDLE, 'Najnovšie pridané filmy')
    xbmcplugin.setContent(ADDON_HANDLE, 'movies')

    skip_count = (page - 1) * PER_PAGE

    movies = mongo_api.get_items(
        "movies",
        query={"status": 1},
        sort={"movieId": -1},
        skip=skip_count,
        limit=PER_PAGE
    )

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

#-------- Filmy s CZ/SK dabingom (podľa najnovšieho streamu) --------
def show_movies_with_cz_audio(page=1):
    xbmcplugin.setPluginCategory(ADDON_HANDLE, 'Filmy s CZ/SK dabingom')
    xbmcplugin.setContent(ADDON_HANDLE, 'movies')

    PER_FETCH = 100  # max počet rôznych filmov (movieId) prednačítaných z movie_detail
    skip_count = (page - 1) * PER_PAGE

    # 1. Získaj 100 najnovších záznamov z movie_detail s CZE/SLO/SLK audio
    pipeline = [
        {
            "$match": {
                "audio": {
                    "$elemMatch": {
                        "$regex": "(CZE|SLO|SLK)",
                        "$options": "i"
                    }
                }
            }
        },
        {
            "$sort": { "_id": -1 }
        },
        {
            "$group": {
                "_id": "$fkMovieId",
                "latestDetailId": { "$first": "$_id" }
            }
        },
        {
            "$sort": { "latestDetailId": -1 }
        },
        {
            "$limit": PER_FETCH
        }
    ]

    result = mongo_api.run_aggregation("movie_detail", pipeline)
    all_movie_ids = [doc["_id"] for doc in result]  # _id tu je fkMovieId

    # 2. Vyber len tie pre aktuálnu stránku
    paged_movie_ids = all_movie_ids[skip_count:skip_count + PER_PAGE]

    if not paged_movie_ids:
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return

    # 3. Načítaj príslušné filmy z kolekcie movies
    movies = mongo_api.get_items(
        "movies",
        query={"movieId": {"$in": paged_movie_ids}, "status": 1}
    )

    # 4. Zoradenie podľa pôvodného poradia
    movies_dict = {m["movieId"]: m for m in movies}
    sorted_movies = [movies_dict[mid] for mid in paged_movie_ids if mid in movies_dict]

    # 5. Navigácia hore
    add_pagination_controls(page, 0, PER_PAGE, action='show_movies_with_cz_audio')

    count = 0
    for movie in sorted_movies:
        count += 1
        add_movie_listitem(movie, ADDON_HANDLE)

    # 6. Navigácia dole
    add_pagination_controls(page, count, PER_PAGE, action='show_movies_with_cz_audio')

    xbmcplugin.endOfDirectory(ADDON_HANDLE)

#-------- Najnovšie seriály --------
def list_latest_series():
    xbmcplugin.setPluginCategory(ADDON_HANDLE, 'Najnovšie seriály')
    xbmcplugin.setContent(ADDON_HANDLE, 'tvshows')

    try:
        # Hlavné menu späť
        url = build_url({'action': 'main_menu'})
        li = xbmcgui.ListItem(label='[B]<< Prejdi na hlavné menu[/B]')
        li.setArt({'icon': 'DefaultFolderBack.png'})
        xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=url, listitem=li, isFolder=True)

        # Najnovšie seriály s epizódami
        series_with_episodes = mongo_api.run_aggregation("episodes", [
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
            add_series_listitem(s, ADDON_HANDLE)

    except Exception as e:
        xbmc.log(f"Error in list_latest_series: {str(e)}", xbmc.LOGERROR)
        xbmcgui.Dialog().notification("Chyba", "Problém s pripojením k API", xbmcgui.NOTIFICATION_ERROR)
    finally:
        xbmcplugin.endOfDirectory(ADDON_HANDLE)

#-------- Najnovšie pridané seriály --------
def list_latest_added_series(page=1):
    xbmcplugin.setPluginCategory(ADDON_HANDLE, 'Najnovšie pridané seriály')
    xbmcplugin.setContent(ADDON_HANDLE, 'tvshows')

    skip_count = (page - 1) * PER_PAGE

    # Get latest 50 series with available episodes
    series_with_episodes = mongo_api.run_aggregation("episodes", [
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
        movie = mongo_api.get_item("movies", "movieId", movie_id)
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
        series = mongo_api.get_item("series", "serieId", serie_id)
        if series:
            add_series_listitem(series, ADDON_HANDLE)

    xbmcplugin.endOfDirectory(ADDON_HANDLE)

#  Seriály
def list_series(page=1):
    xbmcplugin.setPluginCategory(ADDON_HANDLE, 'Seriály')
    xbmcplugin.setContent(ADDON_HANDLE, 'tvshows')

    skip_count = (page - 1) * PER_PAGE

    series_with_episodes = mongo_api.run_aggregation("episodes", [
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
        serie_info = mongo_api.get_item("series", "serieId", serieId)

        all_seasons =  mongo_api.get_items("seasons", query={"serieId": serieId})

        available_counts = mongo_api.run_aggregation("episodes", [
            {"$match": {"serieId": serieId, "statusWS": 1}},
            {"$group": {"_id": "$seasonId", "count": {"$sum": 1}}}
        ])
        
        available_dict = {s["_id"]: s["count"] for s in available_counts}

        for season in all_seasons:
            season_id = season["seasonId"]
            season_num = season["season_number"]
            name = season.get("name") or f"Sezóna {season_num}"
            air_date = season.get("air_date", "")
            year = air_date[:4] if air_date else ""
            ep_count = available_dict.get(season_id, 0)

            label = f"[B]{name}[/B]"
            if year:
                label += f" [COLOR grey]({year})[/COLOR]"
            if ep_count > 0:
                label += f" [COLOR lightgreen]{ep_count} epizód[/COLOR]"

            extra_info = {
                'mediatype': 'season',
                'tvshowtitle': serie_info.get("title", ""),
                'premiered': air_date,
                'season': season_num,
                'episode': season.get("episode_count", 0)
            }

            li = create_compatible_list_item(
                title=name,
                plot=season.get("overview", ""),
                year=year,
                thumb_url=season.get("posterUrl") or serie_info.get("posterUrl", "DefaultTVShows.png"),
                extra_info=extra_info
            )
            li.setLabel(label)

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

        serie_info = mongo_api.get_item("series", "serieId", serieId)
        season_data = mongo_api.get_item("seasons", "seasonId", seasonId)
        season_num = season_data.get("season_number", 1) if season_data else 1

        episodes = mongo_api.get_items(  # Priamy dotaz
            "episodes",
            query={"serieId": serieId, "seasonId": seasonId, "statusWS": 1},
            sort={"episode_number": 1}
        )

        for episode in episodes:
            ep_num = episode.get("episode_number", 0)
            ep_title = episode.get("name", f"Epizóda {ep_num}")
            aired = episode.get("air_date")
            
            # Fix for None air_date
            if aired:
                try:
                    aired_str = datetime.strptime(aired, '%Y-%m-%d').strftime('%d.%m.%Y')
                except:
                    aired_str = aired  # fallback to original format if parsing fails
            else:
                aired_str = ""

            rating = episode.get("vote_average", 0)
            runtime = episode.get("runtime", 0)

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

            extra_info = {
                'mediatype': 'episode',
                'tvshowtitle': serie_info.get("title", ""),
                'season': season_num,
                'episode': ep_num,
                'premiered': aired,
                'rating': float(rating) if rating else None,
                'duration': int(runtime) * 60 if runtime else None  # Convert to seconds
            }

            li = create_compatible_list_item(
                title=ep_title,
                plot=episode.get("overview", "Bez popisu."),
                year=aired[:4] if aired else None,
                thumb_url=episode.get("stillUrl") or season_data.get("posterUrl") or serie_info.get("posterUrl", "DefaultTVShows.png"),
                extra_info=extra_info
            )
            li.setLabel(label)
            li.setProperty('IsPlayable', 'true')

            url = build_url({'action': 'select_stream_serie', 'episodeId': episode["episodeId"]})
            xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=url, listitem=li, isFolder=False)

    finally:
        xbmcplugin.endOfDirectory(ADDON_HANDLE)

def list_watch_later():
    xbmcplugin.setPluginCategory(ADDON_HANDLE, 'Pozrieť si neskôr')
    xbmcplugin.setContent(ADDON_HANDLE, 'videos')
    items = load_watch_later()

    for entry in items:
        if entry['type'] == 'movie':
            movie = mongo_api.get_item("movies", "movieId", entry['id'])
            if movie:
                context_items = [(
                    "Odstrániť zo zoznamu",
                    f'RunPlugin({build_url({"action": "remove_watch_later", "type": "movie", "id": entry["id"]})})'
                )]
                add_movie_listitem(movie, ADDON_HANDLE, context_items)
        elif entry['type'] == 'serie':
            series = mongo_api.get_item("series", "serieId", entry['id'])
            if series:
                context_items = [(
                    "Odstrániť zo zoznamu",
                    f'RunPlugin({build_url({"action": "remove_watch_later", "type": "serie", "id": entry["id"]})})'
                )]
                add_series_listitem(series, ADDON_HANDLE, context_items)

    xbmcplugin.endOfDirectory(ADDON_HANDLE)

def set_video_info_tag(li, info):
    try:
        tag = li.getVideoInfoTag()
        
        # Basic info
        tag.setTitle(info.get('title', ''))
        tag.setPlot(info.get('plot', ''))
        
        # Handle numeric values
        if 'year' in info and info['year']:
            try:
                tag.setYear(int(info['year']))
            except (ValueError, TypeError):
                pass
        
        # Handle media type
        tag.setMediaType(info.get('mediatype', 'video'))
        
        # TV Show specific
        if info.get('mediatype') == 'tvshow':
            tag.setTvShowTitle(info.get('tvshowtitle', ''))
        
        # Season/episode info
        if 'season' in info:
            try:
                tag.setSeason(int(info['season']))
            except (ValueError, TypeError):
                pass
                
        if 'episode' in info:
            try:
                tag.setEpisode(int(info['episode']))
            except (ValueError, TypeError):
                pass
        
        # Dates
        if 'premiered' in info and info['premiered']:
            tag.setPremiered(info['premiered'])
        
        # Rating
        if 'rating' in info and info['rating'] is not None:
            try:
                tag.setRating(float(info['rating']), 0)
            except (ValueError, TypeError):
                pass
        
        # Duration (in seconds)
        if 'duration' in info and info['duration']:
            try:
                tag.setDuration(int(info['duration']))
            except (ValueError, TypeError):
                pass
        
        # Genres
        if 'genre' in info:
            if isinstance(info['genre'], list):
                tag.setGenres(info['genre'])
            elif isinstance(info['genre'], str):
                tag.setGenres([g.strip() for g in info['genre'].split(',') if g.strip()])
                
    except Exception as e:
        xbmc.log(f"[Demostream] InfoTag error: {str(e)}", xbmc.LOGERROR)

def select_stream(movie_id):
    try:
        # Validate input
        try:
            movie_id = int(movie_id)
        except (ValueError, TypeError):
            xbmc.log(f"[Demostream] Invalid movie_id: {movie_id}", xbmc.LOGERROR)
            xbmcgui.Dialog().notification("Chyba", "Neplatné ID filmu", xbmcgui.NOTIFICATION_ERROR)
            return

        # Disable hardware acceleration to prevent crashes
        #xbmc.executebuiltin('SetSetting(video.accel.method,0)')
        #xbmc.sleep(200)  # Allow settings to apply

        # Get movie details with error handling
        try:
            movie_info = mongo_api.get_item("movies", "movieId", movie_id)
            if not movie_info:
                raise ValueError("Movie not found in database")
        except Exception as e:
            xbmc.log(f"[Demostream] Database error: {str(e)}", xbmc.LOGERROR)
            xbmcgui.Dialog().notification("Chyba", "Nepodarilo sa načítať informácie o filme", xbmcgui.NOTIFICATION_ERROR)
            return

        # Get available streams with error handling
        try:
            details = list(mongo_api.get_items("movie_detail", query={"fkMovieId": movie_id}))
            if not details:
                xbmcgui.Dialog().notification("Info", "Pre tento film nie sú dostupné žiadne streamy", xbmcgui.NOTIFICATION_INFO)
                return
        except Exception as e:
            xbmc.log(f"[Demostream] Stream query failed: {str(e)}", xbmc.LOGERROR)
            xbmcgui.Dialog().notification("Chyba", "Chyba pri načítaní streamov", xbmcgui.NOTIFICATION_ERROR)
            return

        # Sort streams
        try:
            dmstrm_files = [f for f in details if "-dmstrm." in f.get("name", "").lower()]
            other_files = [f for f in details if "-dmstrm." not in f.get("name", "").lower()]
            
            def sort_key(file):
                return (
                    -resolution_order.get(file.get("resolution", ""), 0),
                    -float(file.get("size", "0").replace(" GB", ""))
                )
            
            dmstrm_files.sort(key=sort_key)
            other_files.sort(key=sort_key)
            sorted_details = dmstrm_files + other_files
        except Exception as e:
            xbmc.log(f"[Demostream] Sorting failed: {str(e)}", xbmc.LOGWARNING)
            sorted_details = details  # Fallback to unsorted list

        # Create selection dialog
        dialog_items = []
        try:
            default_thumb = movie_info.get('posterUrl', 'DefaultAddonVideo.png')
            for file in sorted_details:
                try:
                    audio = file.get("audio", ["Neznámy"])
                    resolution = file.get("resolution", "N/A")
                    size = file.get("size", "N/A")
                    bitrate = file.get("bitrate", "N/A")
                    codec = file.get("videoCodec", "N/A")
                    filename = file.get("name", "N/A")

                    display_filename = "[COLOR FFFF8800]Demo[/COLOR][COLOR FFFFFFFF]Stream[/COLOR]" if "-dmstrm." in filename.lower() else f"[COLOR FFFFFFFF]{filename}[/COLOR]"

                    li = xbmcgui.ListItem(label=f"[B]{resolution}[/B] • [COLOR FFFFCC00]{', '.join(audio)}[/COLOR] • [COLOR FF00FF00]{size}[/COLOR] • {bitrate} • {codec}")
                    li.setLabel2(display_filename)
                    li.setArt({'thumb': default_thumb, 'icon': 'DefaultAddonVideo.png'})
                    dialog_items.append(li)
                except Exception as e:
                    xbmc.log(f"[Demostream] Error creating listitem: {str(e)}", xbmc.LOGWARNING)
                    continue
        except Exception as e:
            xbmc.log(f"[Demostream] Dialog creation failed: {str(e)}", xbmc.LOGERROR)
            xbmcgui.Dialog().notification("Chyba", "Chyba pri vytváraní výberu", xbmcgui.NOTIFICATION_ERROR)
            return

        # Show selection dialog
        try:
            dialog = xbmcgui.Dialog()
            index = dialog.select("Vyberte stream: [COLOR FFFFCC00]Rozlišenie • Veľkosť[/COLOR]", dialog_items, useDetails=True)
            if index < 0:
                return  # User cancelled
        except Exception as e:
            xbmc.log(f"[Demostream] Dialog selection failed: {str(e)}", xbmc.LOGERROR)
            return

        # Get selected stream URL
        try:
            selected_ident = sorted_details[index].get("ident")
            if not selected_ident:
                raise ValueError("Invalid stream ident")
                
            play_url = get_ws().get_stream_url(ident=selected_ident, mongo_collection="movie_detail")
            
            error_messages = {
                "deleted": ("Vymazané", "Súbor bol vymazaný", xbmcgui.NOTIFICATION_INFO),
                "unauthorized": ("Chyba", "Nemáte oprávnenie", xbmcgui.NOTIFICATION_ERROR),
                "not_found": ("Chyba", "Súbor neexistuje", xbmcgui.NOTIFICATION_ERROR),
                "password_protected": ("Chyba", "Súbor je zaheslovaný", xbmcgui.NOTIFICATION_ERROR),
                None: ("Chyba", "Nepodarilo sa získať stream", xbmcgui.NOTIFICATION_ERROR)
            }

            if play_url in error_messages:
                title, msg, icon = error_messages[play_url]
                xbmcgui.Dialog().notification(title, msg, icon)
                return
        except Exception as e:
            xbmc.log(f"[Demostream] Stream URL fetch failed: {str(e)}", xbmc.LOGERROR)
            xbmcgui.Dialog().notification("Chyba", "Chyba pri získavaní streamu", xbmcgui.NOTIFICATION_ERROR)
            return

        # Play the stream with full error handling
        try:
            # Create list item with metadata
            list_item = create_compatible_list_item(
                movie_info.get("title", "Neznámy film"),
                movie_info.get("overview", ""),
                movie_info.get("year", 0),
                default_thumb,
                {
                    "genre": ", ".join([get_genre_dict().get(str(gid), "") for gid in movie_info.get("genres", [])]),
                    "rating": float(movie_info.get("vote_average", 0)) if movie_info.get("vote_average") else None
                }
            )
            list_item.setPath(play_url)
            
            # Stop any existing playback
            if xbmc.Player().isPlaying():
                xbmc.Player().stop()
                xbmc.sleep(500)
                
            # Small delay before playback
            xbmc.sleep(300)
            
            # Set resolved URL
            xbmcplugin.setResolvedUrl(ADDON_HANDLE, True, list_item)
            
            # Save to history
            save_played_movie(movie_id)
            
            # Small delay after playback starts
            xbmc.sleep(700)
            
        except Exception as e:
            xbmc.log(f"[Demostream] Playback failed: {str(e)}", xbmc.LOGERROR)
            xbmcgui.Dialog().notification("Chyba", "Chyba pri spustení prehrávania", xbmcgui.NOTIFICATION_ERROR)
            try:
                if xbmc.Player().isPlaying():
                    xbmc.Player().stop()
                    xbmc.sleep(500)
            except:
                pass
            return

    except Exception as e:
        xbmc.log(f"[Demostream] FATAL ERROR in select_stream: {str(e)}", xbmc.LOGERROR)
        xbmcgui.Dialog().notification("Kritická chyba", "Došlo k neočakávanej chybe", xbmcgui.NOTIFICATION_ERROR)
    finally:
        # Cleanup
        if xbmcgui.getCurrentWindowDialogId() != -1:
            xbmc.executebuiltin('Dialog.Close(all,true)')

def select_stream_serie(episodeId):
    try:
        # Validate input
        try:
            episodeId = int(episodeId)
        except (ValueError, TypeError):
            xbmc.log(f"[Demostream] Invalid episodeId: {episodeId}", xbmc.LOGERROR)
            xbmcgui.Dialog().notification("Chyba", "Neplatné ID epizódy", xbmcgui.NOTIFICATION_ERROR)
            return

        # Disable hardware acceleration
        #xbmc.executebuiltin('SetSetting(video.accel.method,0)')
        #xbmc.sleep(200)

        # Get episode details with error handling
        try:
            details = list(mongo_api.get_items("episode_detail_links", query={"episodeId": episodeId}))
            if not details:
                xbmcgui.Dialog().notification("Info", "Pre túto epizódu nie sú dostupné streamy", xbmcgui.NOTIFICATION_INFO)
                return
        except Exception as e:
            xbmc.log(f"[Demostream] Episode query failed: {str(e)}", xbmc.LOGERROR)
            xbmcgui.Dialog().notification("Chyba", "Chyba pri načítaní epizódy", xbmcgui.NOTIFICATION_ERROR)
            return

        # Sort streams
        try:
            dmstrm_files = [f for f in details if "-dmstrm." in f.get("name", "").lower()]
            other_files = [f for f in details if "-dmstrm." not in f.get("name", "").lower()]
            
            def sort_key(file):
                return (
                    -resolution_order.get(file.get("resolution", ""), 0),
                    -float(file.get("size", "0").replace(" GB", ""))
                )
            
            dmstrm_files.sort(key=sort_key)
            other_files.sort(key=sort_key)
            sorted_details = dmstrm_files + other_files
        except Exception as e:
            xbmc.log(f"[Demostream] Sorting failed: {str(e)}", xbmc.LOGWARNING)
            sorted_details = details

        # Get metadata for the episode
        try:
            episode = mongo_api.get_item("episodes", "episodeId", episodeId)
            if not episode:
                raise ValueError("Episode not found")
                
            serie_info = mongo_api.get_item("series", "serieId", episode.get("serieId")) if episode else None
            season = mongo_api.get_item("seasons", "seasonId", episode.get("seasonId")) if episode else None
            
            thumb = (episode.get("stillUrl") or 
                    (serie_info.get("posterUrl", "DefaultAddonVideo.png") if serie_info else "DefaultAddonVideo.png"))
        except Exception as e:
            xbmc.log(f"[Demostream] Metadata fetch failed: {str(e)}", xbmc.LOGERROR)
            thumb = "DefaultAddonVideo.png"
            serie_info = None
            episode = None

        # Create selection dialog
        dialog_items = []
        try:
            for file in sorted_details:
                try:
                    audio = file.get("audio", ["Neznámy"])
                    resolution = file.get("resolution", "?")
                    size = file.get("size", "?")
                    bitrate = file.get("bitrate", "?")
                    codec = file.get("videoCodec", "?")
                    name = file.get("name", "N/A")

                    display_filename = "[COLOR FFFF8800]Demo[/COLOR][COLOR FFFFFFFF]Stream[/COLOR]" if "-dmstrm." in name.lower() else f"[COLOR FFFFFFFF]{name}[/COLOR]"

                    li = xbmcgui.ListItem(label=f"[B]{resolution}[/B] • [COLOR FFFFCC00]{', '.join(audio)}[/COLOR] • [COLOR FF00FF00]{size}[/COLOR] • {bitrate} • {codec}")
                    li.setLabel2(display_filename)
                    li.setArt({'thumb': thumb, 'icon': 'DefaultAddonVideo.png'})
                    dialog_items.append(li)
                except Exception as e:
                    xbmc.log(f"[Demostream] Error creating dialog item: {str(e)}", xbmc.LOGWARNING)
                    continue
        except Exception as e:
            xbmc.log(f"[Demostream] Dialog creation failed: {str(e)}", xbmc.LOGERROR)
            xbmcgui.Dialog().notification("Chyba", "Chyba pri vytváraní výberu", xbmcgui.NOTIFICATION_ERROR)
            return

        # Show selection dialog
        try:
            dialog = xbmcgui.Dialog()
            index = dialog.select("Vyberte stream: [COLOR FFFFCC00]Rozlišenie • Veľkosť[/COLOR]", dialog_items, useDetails=True)
            if index < 0:
                return  # User cancelled
        except Exception as e:
            xbmc.log(f"[Demostream] Dialog selection failed: {str(e)}", xbmc.LOGERROR)
            return

        # Get selected stream URL
        try:
            selected_ident = sorted_details[index].get("ident")
            if not selected_ident:
                raise ValueError("Invalid stream ident")
                
            play_url = get_ws().get_stream_url(ident=selected_ident, mongo_collection="episode_detail_links")
            
            error_messages = {
                "deleted": ("Vymazané", "Súbor bol vymazaný", xbmcgui.NOTIFICATION_INFO),
                "unauthorized": ("Chyba", "Nemáte oprávnenie", xbmcgui.NOTIFICATION_ERROR),
                "not_found": ("Chyba", "Súbor neexistuje", xbmcgui.NOTIFICATION_ERROR),
                "password_protected": ("Chyba", "Súbor je zaheslovaný", xbmcgui.NOTIFICATION_ERROR),
                None: ("Chyba", "Nepodarilo sa získať stream", xbmcgui.NOTIFICATION_ERROR)
            }

            if play_url in error_messages:
                title, msg, icon = error_messages[play_url]
                xbmcgui.Dialog().notification(title, msg, icon)
                return
        except Exception as e:
            xbmc.log(f"[Demostream] Stream URL fetch failed: {str(e)}", xbmc.LOGERROR)
            xbmcgui.Dialog().notification("Chyba", "Chyba pri získavaní streamu", xbmcgui.NOTIFICATION_ERROR)
            return

        # Play the stream with full error handling
        try:
            # Prepare metadata
            label = episode.get("name", "Epizóda") if episode else "Epizóda"
            year = serie_info.get("year", 0) if serie_info else 0
            plot = episode.get("overview", "") if episode else ""
            
            extra_info = {
                "season": season.get("season_number", 1) if season else 1,
                "episode": episode.get("episode_number", 1) if episode else 1,
                "tvshowtitle": serie_info.get("title", "") if serie_info else "",
                "genre": ", ".join([get_genre_dict().get(str(gid), "") for gid in serie_info.get("genres", [])]) if serie_info else "",
                "plot": plot,
                "mediatype": "episode"
            }

            # Create list item
            list_item = create_compatible_list_item(
                label, 
                plot, 
                year, 
                thumb, 
                extra_info
            )
            list_item.setPath(play_url)
            
            # Stop any existing playback
            if xbmc.Player().isPlaying():
                xbmc.Player().stop()
                
            # Small delay before playback
            xbmc.sleep(300)
            
            # Set resolved URL
            xbmcplugin.setResolvedUrl(ADDON_HANDLE, True, list_item)
            
            # Save to history
            if serie_info:
                save_played_series(serie_info.get("serieId"))
            
            # Small delay after playback starts
            xbmc.sleep(700)
            
        except Exception as e:
            xbmc.log(f"[Demostream] Playback failed: {str(e)}", xbmc.LOGERROR)
            xbmcgui.Dialog().notification("Chyba", "Chyba pri spustení prehrávania", xbmcgui.NOTIFICATION_ERROR)
            try:
                if xbmc.Player().isPlaying():
                    xbmc.Player().stop()
            except:
                pass
            return

    except Exception as e:
        xbmc.log(f"[Demostream] FATAL ERROR in select_stream_serie: {str(e)}", xbmc.LOGERROR)
        xbmcgui.Dialog().notification("Kritická chyba", "Došlo k neočakávanej chybe", xbmcgui.NOTIFICATION_ERROR)
    finally:
        # Cleanup
        if xbmcgui.getCurrentWindowDialogId() != -1:
            xbmc.executebuiltin('Dialog.Close(all,true)')

# CSFD typy na dnes
def typy_na_dnes_csfd():
    xbmcplugin.setPluginCategory(ADDON_HANDLE, 'Tipy na dnes (ČSFD)')
    xbmcplugin.setContent(ADDON_HANDLE, 'movies')

    def fetch_csfd_tips():
        # Najprv získať len základné dáta z ČSFD
        results = fetch_csfd_tip_titles()
        if not results or (isinstance(results, dict) and results.get("error")):
            return []

        # Cache jednotlivých filmov
        movies = []
        for item in results:
            cache_key = f"movie:{item['title']}:{item['year']}"
            movie = redis_cache.get_or_cache(
                cache_key,
                lambda: mongo_api.get_item("movies", "title", item["title"], query={"year": item["year"]}),
                ttl=86400  # 1 deň pre jednotlivé filmy
            )
            
            if movie and movie.get("status") == 1:
                movies.append(movie)
        return movies

    # Cache pre celý zoznam tipov
    movies = redis_cache.get_or_cache(
        "csfd_tips_list",
        fetch_csfd_tips,
        ttl=21600  # 6 hodín pre celý zoznam
    )

    if not movies:
        xbmcgui.Dialog().notification("Chyba", "Nenašli sa žiadne filmy", xbmcgui.NOTIFICATION_WARNING)
    else:
        for movie in movies:
            add_movie_listitem(movie, ADDON_HANDLE)

    xbmcplugin.endOfDirectory(ADDON_HANDLE)

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
        # Cache pre písmená A-Z a 0-9
        letters_data = redis_cache.get_or_cache(
            "movies_initials_summary",
            lambda: get_initials_summary(),
            ttl=86400  # 24 hodín
        )
        
        for letter, count in letters_data.items():
            if count > 0:
                url = build_url({'action': 'list_movies_by_name', 'initial': letter, 'length': 1})
                li = xbmcgui.ListItem(label=f'{letter} ({count})')
                xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=url, listitem=li, isFolder=True)
    
    elif length == 1:
        # Cache pre dvojice písmen
        two_chars_data = redis_cache.get_or_cache(
            f"movies_two_chars_{initial}",
            lambda: get_two_chars_combinations(initial),
            ttl=86400  # 24 hodín
        )
        
        for two_chars, count in two_chars_data.items():
            url = build_url({'action': 'list_movies_by_name', 'initial': two_chars, 'length': 2})
            li = xbmcgui.ListItem(label=f'{two_chars} ({count})')
            xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=url, listitem=li, isFolder=True)
    
    elif length == 2:
        # Cache pre trojice písmen
        three_chars_data = redis_cache.get_or_cache(
            f"movies_three_chars_{initial}",
            lambda: get_three_chars_combinations(initial),
            ttl=86400  # 24 hodín
        )
        
        for three_chars, count in three_chars_data.items():
            url = build_url({'action': 'list_movies_by_name', 'initial': three_chars, 'length': 3})
            li = xbmcgui.ListItem(label=f'{three_chars} ({count})')
            xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=url, listitem=li, isFolder=True)
    
    else:
        # Zobraziť filmy pre zvolenú trojicu písmen
        movies = get_movies_by_initial(initial, length=3)
        for movie in movies:
            add_movie_listitem(movie, ADDON_HANDLE)
    
    xbmcplugin.endOfDirectory(ADDON_HANDLE)

def get_initials_summary():
    """Získaj súhrn počtov filmov pre každé písmeno pomocou agregácie"""
    pipeline = [
        {"$match": {"status": 1}},
        {"$project": {
            "first_char": {"$substrCP": [{"$toUpper": "$title"}, 0, 1]}
        }},
        {"$match": {
            "first_char": {"$regex": "^[A-Z0-9]$"}
        }},
        {"$group": {
            "_id": "$first_char",
            "count": {"$sum": 1}
        }},
        {"$sort": {"_id": 1}}
    ]
    
    result = {}
    try:
        items = mongo_api.run_aggregation("movies", pipeline)
        for item in items:
            result[item["_id"]] = item["count"]
    except Exception as e:
        xbmc.log(f"Chyba pri získavaní súhrnu iniciál: {str(e)}", xbmc.LOGERROR)
        # Fallback - ručné získanie pre každé písmeno
        letters = [chr(i) for i in range(65, 91)] + [str(i) for i in range(10)]
        for letter in letters:
            movies = get_movies_by_initial(letter)
            count = len(movies)
            if count > 0:
                result[letter] = count
    
    return result

def get_two_chars_combinations(initial):
    """Získaj kombinácie prvých dvoch písmen a ich počty"""
    movies = get_movies_by_initial(initial)
    two_chars = {}
    
    for movie in movies:
        first_two = movie['title'][:2].upper()
        two_chars[first_two] = two_chars.get(first_two, 0) + 1
    
    return two_chars

def get_three_chars_combinations(initial):
    """Získaj kombinácie prvých troch písmen a ich počty"""
    movies = get_movies_by_initial(initial)
    three_chars = {}
    
    for movie in movies:
        first_three = movie['title'][:3].upper()
        three_chars[first_three] = three_chars.get(first_three, 0) + 1
    
    return three_chars

def get_movies_by_initial(initial, length=1):
    """Získaj filmy začínajúce na dané písmeno/znaky"""
    cache_key = f"movies_initial_{initial}_{length}"
    
    return redis_cache.get_or_cache(
        cache_key,
        lambda: mongo_api.get_items(
            "movies",
            query={
                "title": {"$regex": f'^{re.escape(initial)}', "$options": "i"}, 
                "status": 1
            },
            sort={"title": 1}
        ),
        ttl=3600  # 1 hodina
    )

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
        # Cache pre písmená A-Z a 0-9
        letters_data = redis_cache.get_or_cache(
            "series_initials_summary",
            lambda: get_series_initials_summary(),
            ttl=3600  # 24 hodín
        )
        
        for letter, count in letters_data.items():
            if count > 0:
                url = build_url({'action': 'list_series_by_name', 'initial': letter, 'length': 1})
                li = xbmcgui.ListItem(label=f'{letter} ({count})')
                xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=url, listitem=li, isFolder=True)
    
    elif length == 1:
        # Cache pre dvojice písmen
        two_chars_data = redis_cache.get_or_cache(
            f"series_two_chars_{initial}",
            lambda: get_series_two_chars_combinations(initial),
            ttl=3600  # 24 hodín
        )
        
        for two_chars, count in two_chars_data.items():
            url = build_url({'action': 'list_series_by_name', 'initial': two_chars, 'length': 2})
            li = xbmcgui.ListItem(label=f'{two_chars} ({count})')
            xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=url, listitem=li, isFolder=True)
    
    elif length == 2:
        # Cache pre trojice písmen
        three_chars_data = redis_cache.get_or_cache(
            f"series_three_chars_{initial}",
            lambda: get_series_three_chars_combinations(initial),
            ttl=3600  # 24 hodín
        )
        
        for three_chars, count in three_chars_data.items():
            if three_chars.startswith(initial.upper()):  # Pridaná kontrola
                url = build_url({'action': 'list_series_by_name', 'initial': three_chars, 'length': 3})
                li = xbmcgui.ListItem(label=f'{three_chars} ({count})')
                xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=url, listitem=li, isFolder=True)
    
    else:
        # Zobraziť seriály pre zvolenú trojicu písmen
        series = get_series_by_initial(initial, length=3)
        for s in series:
            add_series_listitem(s, ADDON_HANDLE)
    
    xbmcplugin.endOfDirectory(ADDON_HANDLE)

def get_series_initials_summary():
    """Získaj súhrn počtov seriálov pre každé písmeno"""
    letters = [chr(i) for i in range(65, 91)] + [str(i) for i in range(10)]
    result = {}
    
    for letter in letters:
        # Použijeme existujúcu funkciu get_series_by_initial s limitom 1
        series = get_series_by_initial(letter, length=1)
        count = len(series) if series else 0
        if count > 0:
            result[letter] = count
    
    return result

def get_series_two_chars_combinations(initial):
    """Získaj kombinácie prvých dvoch písmen a ich počty pre seriály"""
    series = get_series_by_initial(initial)
    two_chars = {}
    
    for s in series:
        first_two = s['title'][:2].upper()
        two_chars[first_two] = two_chars.get(first_two, 0) + 1
    
    return two_chars

def get_series_three_chars_combinations(initial):
    """Získaj kombinácie prvých troch písmen a ich počty pre seriály"""
    series = get_series_by_initial(initial)
    three_chars = {}
    
    for s in series:
        if len(s['title']) >= 3:  # Pridaná kontrola dĺžky názvu
            first_three = s['title'][:3].upper()
            three_chars[first_three] = three_chars.get(first_three, 0) + 1
    
    return three_chars

def get_series_by_initial(initial, length=1):
    """Získaj seriály začínajúce na dané písmeno/znaky"""
    cache_key = f"series_initial_{initial}_{length}"
    
    return redis_cache.get_or_cache(
        cache_key,
        lambda: mongo_api.run_aggregation("series", [
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
        ]),
        ttl=3600  # 1h
    )

def show_latest_dubbed_movies():
    xbmcplugin.setPluginCategory(ADDON_HANDLE, 'Novinky dabované')
    xbmcplugin.setContent(ADDON_HANDLE, 'movies')

    def fetch_dubbed_movies():
        """Získanie dabovaných filmov s novou štruktúrou stream_audio"""
        dubbed_langs = {"CZE", "CSE", "SLK"}
        
        movies = mongo_api.get_items(
            "movies",
            query={
                "status": 1,
                "release_date": {"$exists": True},
                "stream_audio": {"$regex": "(CZE|CSE|SLK)", "$options": "i"}
            },
            sort={"release_date": -1},
            limit=100
        )
        
        filtered = []
        for movie in movies:
            audio_str = movie.get("stream_audio", "")
            if not audio_str:
                continue
                
            audio_codes = {code.strip().upper() for code in audio_str.split(",")}
            if audio_codes & dubbed_langs:
                filtered.append(movie)
        
        return filtered[:100]

    movies = redis_cache.get_or_cache("latest_dubbed_movies", fetch_dubbed_movies, ttl=600)

    for movie in movies:
        add_movie_listitem(movie, ADDON_HANDLE)

    xbmcplugin.endOfDirectory(ADDON_HANDLE)

def list_top_popular_movies_czsk():
    xbmcplugin.setPluginCategory(ADDON_HANDLE, 'Top 100 populárnych filmov (CZ/SK)')
    xbmcplugin.setContent(ADDON_HANDLE, 'movies')

    def fetch_popular_movies():
        # 1. Získanie populárnych ID z TMDB
        api_key = addon.getSetting("tmdb_api_key")
        popular_ids = []
        
        # Paralelné načítanie stránok
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = []
            for page in range(1, 6):
                url = f"https://api.themoviedb.org/3/movie/popular?api_key={api_key}&language=cs-CZ&region=CZ&page={page}"
                futures.append(executor.submit(fetch_tmdb_page, url))
            
            for future in as_completed(futures):
                try:
                    data = future.result()
                    popular_ids.extend(item["id"] for item in data.get("results", []))
                except Exception as e:
                    xbmc.log(f"Chyba pri načítaní stránky: {e}", xbmc.LOGWARNING)

        # 2. Hromadné načítanie filmov z MongoDB
        if not popular_ids:
            return []
            
        query = {
            "tmdbId": {"$in": popular_ids[:100]},  # Obmedzenie na prvých 100 ID
            "status": 1
        }
        
        movies = list(mongo_api.get_items(
            "movies",
            query=query,
            sort={"popularity": -1},  # Zoradenie podľa popularity
            limit=100
        ))
        
        return movies

    # Cache celej funkcie s výslednými filmami
    movies = redis_cache.get_or_cache(
        "tmdb_popular_czsk_movies_full",
        fetch_popular_movies,
        ttl=21600  # 6 hodín
    )

    if not movies:
        xbmcgui.Dialog().notification("Chyba", "Nenašli sa žiadne filmy", xbmcgui.NOTIFICATION_WARNING)
    else:
        for movie in movies:
            add_movie_listitem(movie, ADDON_HANDLE)

    xbmcplugin.endOfDirectory(ADDON_HANDLE)

def fetch_tmdb_page(url):
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read().decode('utf-8'))

def list_trending_movies_last_14_days():
    xbmcplugin.setPluginCategory(ADDON_HANDLE, 'Trending filmy (posledných 14 dní)')
    xbmcplugin.setContent(ADDON_HANDLE, 'movies')

    def fetch_trending_movies():
        api_key = addon.getSetting("tmdb_api_key")
        trending_ids = set()
        
        # Paralelné načítanie trendov pre day/week
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = []
            
            # 2 stránky pre denné trendy (day)
            for page in range(1, 3):
                url = f"https://api.themoviedb.org/3/trending/movie/day?api_key={api_key}&page={page}"
                futures.append(executor.submit(fetch_tmdb_page, url))
            
            # 2 stránky pre týždenné trendy (week)
            for page in range(1, 3):
                url = f"https://api.themoviedb.org/3/trending/movie/week?api_key={api_key}&page={page}"
                futures.append(executor.submit(fetch_tmdb_page, url))
            
            for future in as_completed(futures):
                try:
                    data = future.result()
                    for item in data.get("results", []):
                        trending_ids.add(item["id"])
                except Exception as e:
                    xbmc.log(f"Chyba pri načítaní trendov: {e}", xbmc.LOGWARNING)

        trending_ids = list(trending_ids)
        if not trending_ids:
            return []

        # Hromadný dotaz do MongoDB
        movies = list(mongo_api.get_items(
            "movies",
            query={
                "tmdbId": {"$in": trending_ids[:200]},  # Bezpečný limit
                "status": 1
            },
            limit=100
        ))

        # Zachovanie popularity trendingu
        movie_dict = {m["tmdbId"]: m for m in movies}
        return [movie_dict[id] for id in trending_ids if id in movie_dict][:100]

    # Cache celej funkcie s výslednými filmami
    movies = redis_cache.get_or_cache(
        "tmdb_trending_14days_full",
        fetch_trending_movies,
        ttl=10800  # 3 hodiny - trending sa mení častejšie
    )

    if not movies:
        xbmcgui.Dialog().notification("Chyba", "Nenašli sa žiadne trending filmy", xbmcgui.NOTIFICATION_WARNING)
    else:
        for movie in movies:
            add_movie_listitem(movie, ADDON_HANDLE)

    xbmcplugin.endOfDirectory(ADDON_HANDLE)
    
def list_top_rated_movies_czsk():
    xbmcplugin.setPluginCategory(ADDON_HANDLE, 'Top 100 najlepšie hodnotených filmov (CZ/SK)')
    xbmcplugin.setContent(ADDON_HANDLE, 'movies')

    def fetch_top_rated_movies():
        api_key = addon.getSetting("tmdb_api_key")
        top_rated_ids = []
        
        # Načítanie IDčiek (paralelne)
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(
                fetch_tmdb_page,
                f"https://api.themoviedb.org/3/movie/top_rated?api_key={api_key}&language=cs-CZ&region=CZ&page={page}"
            ) for page in range(1, 6)]
            
            for future in as_completed(futures):
                try:
                    data = future.result()
                    top_rated_ids.extend(item["id"] for item in data.get("results", []))
                except Exception as e:
                    xbmc.log(f"Chyba pri načítaní: {e}", xbmc.LOGWARNING)

        if not top_rated_ids:
            return []

        # Hromadný dotaz bez sortu
        movies = list(mongo_api.get_items(
            "movies",
            query={
                "tmdbId": {"$in": top_rated_ids[:100]},
                "status": 1
            },
            limit=100
        ))

        # Jednoduché zoradenie podľa TMDB poradia
        movies.sort(key=lambda m: top_rated_ids.index(m["tmdbId"]))
        return movies[:100]

    # Cache s 6-hodinovou expiráciou
    movies = redis_cache.get_or_cache(
        "tmdb_top_rated_czsk_movies_full", 
        fetch_top_rated_movies,
        ttl=21600
    )

    if not movies:
        xbmcgui.Dialog().notification("Chyba", "Nenašli sa žiadne filmy", xbmcgui.NOTIFICATION_WARNING)
    else:
        for movie in movies:
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
elif action == 'show_movies_with_cz_audio':
    page = int(params.get('page', '1'))
    show_movies_with_cz_audio(page=page)
elif action == 'list_top_popular_movies_czsk':
    list_top_popular_movies_czsk()
elif action == 'list_trending_movies_last_14_days':
    list_trending_movies_last_14_days()
elif action == 'list_top_rated_movies_czsk':
    list_top_rated_movies_czsk()
elif action == 'add_watch_later':
    add_to_watch_later(params.get('type'), int(params.get('id')))
elif action == 'remove_watch_later':
    remove_from_watch_later(params.get('type'), int(params.get('id')))
elif action == 'list_watch_later':
    list_watch_later()
else:
    # Initial login and main menu
    main_menu()