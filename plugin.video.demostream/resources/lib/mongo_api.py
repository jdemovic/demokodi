# resources/lib/mongo_api.py
import urllib.parse
import urllib.request
import json
import xbmc
import socket
import time
from typing import Any
from functools import wraps

#API_BASE = "https://web.demolator.app/mongo"
API_BASE = "http://91.99.208.89:10000/mongo"
MAX_RETRIES = 3
RETRY_DELAY = 1  # sekundy
NETWORK_TIMEOUT = 30  # sekundy

def handle_errors(func):
    """Dekorátor pre jednotnú chybovú obsluhu"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                return func(*args, **kwargs)
            except urllib.error.HTTPError as e:
                try:
                    error_detail = json.loads(e.read().decode('utf-8')).get('detail', str(e))
                except json.JSONDecodeError:
                    error_detail = str(e)
                last_error = f"HTTP error {e.code}: {error_detail}"
                if e.code in (500, 502, 503, 504) and attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY * (attempt + 1))
                    continue
                break
            except urllib.error.URLError as e:
                last_error = f"URL error: {str(e)}"
                if isinstance(e.reason, socket.timeout) and attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY * (attempt + 1))
                    continue
                break
            except json.JSONDecodeError as e:
                last_error = f"JSON decode error: {str(e)}"
                break
            except Exception as e:
                last_error = f"Unexpected error: {str(e)}"
                break
        
        xbmc.log(f"[DemoStream] API {func.__name__} failed after {MAX_RETRIES} attempts: {last_error}", xbmc.LOGERROR)
        return None if func.__name__ in ('get_item', 'insert_item', 'update_item', 'delete_item') else []
    return wrapper

def check_connection():
    """Skontroluje dostupnosť API"""
    try:
        url = f"{API_BASE}/health"
        req = urllib.request.Request(url, method="GET")
        req.timeout = 5  # Krátky timeout pre health check
        
        with urllib.request.urlopen(req) as response:
            return response.getcode() == 200
    except Exception as e:
        xbmc.log(f"[DemoStream] API connection check failed: {str(e)}", xbmc.LOGERROR)
        return False

def wait_for_connection(max_wait=30):
    """Čaká na obnovenie pripojenia"""
    start_time = time.time()
    while time.time() - start_time < max_wait:
        if check_connection():
            return True
        time.sleep(2)
    return False

@handle_errors
def get_items(collection, query=None, sort=None, skip=0, limit=None):
    """Získaj zoznam položiek z kolekcie pomocou POST"""
    url = f"{API_BASE}/items"
    payload = {
        "collection": collection,
        "query": query or {},
        "skip": skip
    }

    if sort:
        payload["sort"] = sort
    if limit is not None:
        payload["limit"] = limit

    post_data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=post_data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.timeout = NETWORK_TIMEOUT
    
    xbmc.log(f"[DemoStream] API get_items request: {url} with data: {post_data.decode('utf-8')}", xbmc.LOGDEBUG)
    
    with urllib.request.urlopen(req) as response:
        if response.getcode() != 200:
            error_msg = f"HTTP {response.getcode()}: {response.read().decode('utf-8')}"
            xbmc.log(f"[DemoStream] API get_items failed: {error_msg}", xbmc.LOGERROR)
            return []
        data = response.read()
        return json.loads(data)

@handle_errors
def get_item(collection: str, field: str, value: Any, query=None):
    """Získaj jednu položku podľa kolekcie, poľa a voliteľného query pomocou POST"""
    url = f"{API_BASE}/item"
    post_data = json.dumps({
        "collection": collection,
        "field": field,
        "value": value,
        "query": query or {}
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=post_data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.timeout = NETWORK_TIMEOUT
    
    xbmc.log(f"[DemoStream] API get_item request: {url} with data: {post_data.decode('utf-8')}", xbmc.LOGDEBUG)
    
    with urllib.request.urlopen(req) as response:
        if response.getcode() != 200:
            error_msg = f"HTTP {response.getcode()}: {response.read().decode('utf-8')}"
            xbmc.log(f"[DemoStream] API get_item failed: {error_msg}", xbmc.LOGERROR)
            return None
        data = response.read()
        return json.loads(data)

@handle_errors
def insert_item(collection: str, data: dict):
    """Vlož novú položku pomocou POST"""
    url = f"{API_BASE}/insert_item"
    json_data = json.dumps({"collection": collection, "data": data}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=json_data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.timeout = NETWORK_TIMEOUT
    
    xbmc.log(f"[DemoStream] API insert_item request: {url} with data: {json_data.decode('utf-8')}", xbmc.LOGDEBUG)
    
    with urllib.request.urlopen(req) as response:
        if response.getcode() != 200:
            error_msg = f"HTTP {response.getcode()}: {response.read().decode('utf-8')}"
            xbmc.log(f"[DemoStream] API insert_item failed: {error_msg}", xbmc.LOGERROR)
            return None
        resp_data = response.read()
        return json.loads(resp_data)

@handle_errors
def update_item(collection: str, field: str, value: str, update_data: dict):
    """Aktualizuj položku podľa poľa pomocou POST"""
    url = f"{API_BASE}/update_item"
    post_data = json.dumps({
        "collection": collection,
        "field": field,
        "value": str(value),
        "update": update_data
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=post_data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.timeout = NETWORK_TIMEOUT
    
    xbmc.log(f"[DemoStream] API update_item request: {url} with data: {post_data.decode('utf-8')}", xbmc.LOGDEBUG)
    
    with urllib.request.urlopen(req) as response:
        if response.getcode() != 200:
            error_msg = f"HTTP {response.getcode()}: {response.read().decode('utf-8')}"
            xbmc.log(f"[DemoStream] API update_item failed: {error_msg}", xbmc.LOGERROR)
            return None
        resp_data = response.read()
        return json.loads(resp_data)

@handle_errors
def delete_item(collection: str, field: str, value: str):
    """Zmaž položku pomocou POST"""
    url = f"{API_BASE}/delete_item"
    post_data = json.dumps({
        "collection": collection,
        "field": field,
        "value": str(value)
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=post_data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.timeout = NETWORK_TIMEOUT
    
    xbmc.log(f"[DemoStream] API delete_item request: {url} with data: {post_data.decode('utf-8')}", xbmc.LOGDEBUG)
    
    with urllib.request.urlopen(req) as response:
        if response.getcode() != 200:
            error_msg = f"HTTP {response.getcode()}: {response.read().decode('utf-8')}"
            xbmc.log(f"[DemoStream] API delete_item failed: {error_msg}", xbmc.LOGERROR)
            return None
        resp_data = response.read()
        return json.loads(resp_data)

@handle_errors
def run_aggregation(collection: str, pipeline: list):
    """Spusti aggregate pipeline pomocou POST"""
    url = f"{API_BASE}/aggregate"
    post_data = json.dumps({"collection": collection, "pipeline": pipeline}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=post_data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.timeout = NETWORK_TIMEOUT
    
    xbmc.log(f"[DemoStream] API run_aggregation request: {url} with pipeline: {json.dumps(pipeline)}", xbmc.LOGDEBUG)
    
    with urllib.request.urlopen(req) as response:
        if response.getcode() != 200:
            error_msg = f"HTTP {response.getcode()}: {response.read().decode('utf-8')}"
            xbmc.log(f"[DemoStream] API run_aggregation failed: {error_msg}", xbmc.LOGERROR)
            return []
        resp_data = response.read()
        result = json.loads(resp_data)
        for doc in result:
            if isinstance(doc.get("_id"), dict):
                doc["_id"] = str(doc["_id"])
        return result

def safe_get_items(collection, query=None, sort=None, skip=0, limit=None):
    """Bezpečná verzia get_items s kontrolou pripojenia"""
    if not check_connection():
        if not wait_for_connection():
            xbmc.log("[DemoStream] Unable to establish connection to API", xbmc.LOGERROR)
            return []
    
    result = get_items(collection, query, sort, skip, limit)
    if result is None:  # V prípade chyby
        return []
    return result