import urllib.parse
import urllib.request
import json
import xbmcaddon
import xbmcgui
import xbmc
from datetime import datetime, timedelta

addon = xbmcaddon.Addon()
TMDB_API_KEY = addon.getSetting('tmdb_api_key')
CACHE_DURATION = timedelta(hours=1)  # Cache results for 1 hour

# Simple cache implementation
_cache = {}
_cache_expiry = {}

def _clear_expired_cache():
    """Remove expired cache entries"""
    now = datetime.now()
    expired_keys = [k for k, exp in _cache_expiry.items() if exp < now]
    for key in expired_keys:
        del _cache[key]
        del _cache_expiry[key]

def search_movie_tmdb(query):
    """Search TMDB using multi endpoint with proper actor search handling"""
    if not query or len(query.strip()) < 2:
        xbmcgui.Dialog().notification("Chyba", "Hľadaný výraz je príliš krátky", xbmcgui.NOTIFICATION_WARNING, 3000)
        return []

    if not TMDB_API_KEY:
        xbmcgui.Dialog().notification("Chyba", "Chýba TMDB API kľúč", xbmcgui.NOTIFICATION_ERROR, 3000)
        return []

    # Check cache first
    cache_key = f"search_{query.lower()}"
    _clear_expired_cache()
    
    if cache_key in _cache:
        xbmc.log(f"Using cached results for query: {query}", xbmc.LOGINFO)
        return _cache[cache_key]

    pDialog = xbmcgui.DialogProgress()
    pDialog.create('Vyhľadávanie', 'Prebieha vyhľadávanie v TMDB...')
    
    results = []
    page = 1
    total_pages = 1

    try:
        while page <= total_pages:
            if pDialog.iscanceled():
                break
                
            pDialog.update(int((page/total_pages)*100), f'Prehľadávam stranu {page} z {total_pages}')

            params = {
                'api_key': TMDB_API_KEY,
                'query': query,
                'language': 'cs-CZ',
                'page': page,
                'include_adult': 'false'
            }
            
            url = 'https://api.themoviedb.org/3/search/multi?' + urllib.parse.urlencode(params)
            xbmc.log(f"TMDB API Request: {url}", xbmc.LOGDEBUG)

            try:
                with urllib.request.urlopen(url, timeout=10) as response:
                    data = response.read()
                    result = json.loads(data)
                    total_pages = result.get('total_pages', 1)
                    
                    for item in result.get('results', []):
                        media_type = item.get('media_type')
                        
                        # Handle person results differently
                        if media_type == 'person':
                            # Get movies/TV shows this person is known for
                            for known_item in item.get('known_for', []):
                                tmdb_id = known_item.get('id')
                                known_media_type = known_item.get('media_type')
                                if tmdb_id and known_media_type in ['movie', 'tv']:
                                    results.append({
                                        'tmdbId': tmdb_id,
                                        'mediaType': known_media_type
                                    })
                        # Handle direct movie/TV show results
                        elif media_type in ['movie', 'tv']:
                            tmdb_id = item.get('id')
                            if tmdb_id:
                                results.append({
                                    'tmdbId': tmdb_id,
                                    'mediaType': media_type
                                })

                        # Early exit if we have enough results
                        if len(results) >= 100:
                            break

                    if len(results) >= 100:
                        break

                page += 1
                
            except urllib.error.HTTPError as e:
                xbmc.log(f"TMDB API Error: {e.code} - {e.reason}", xbmc.LOGERROR)
                if e.code == 401:
                    xbmcgui.Dialog().notification("Chyba", "Neplatný TMDB API kľúč", xbmcgui.NOTIFICATION_ERROR, 3000)
                break
            except Exception as e:
                xbmc.log(f"Error during TMDB search: {str(e)}", xbmc.LOGERROR)
                break

    finally:
        pDialog.close()

    _cache_results(cache_key, results)
    return results

def _cache_results(key, results):
    """Store results in cache with expiration"""
    _cache[key] = results
    _cache_expiry[key] = datetime.now() + CACHE_DURATION
    xbmc.log(f"Cached {len(results)} results for key: {key}", xbmc.LOGDEBUG)