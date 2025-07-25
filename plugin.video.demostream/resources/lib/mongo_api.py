# resources/lib/mongo_api.py
import urllib.parse
import urllib.request
import json
import xbmc
from typing import Any

API_BASE = "https://web.demolator.app/mongo"

def get_items(collection, query=None, sort=None, skip=0, limit=None):
    """Získaj zoznam položiek z kolekcie pomocou POST"""
    try:
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
        
        xbmc.log(f"[SC3] API get_items request: {url} with data: {post_data.decode('utf-8')}", xbmc.LOGDEBUG)
        
        with urllib.request.urlopen(req) as response:
            if response.getcode() != 200:
                error_msg = f"HTTP {response.getcode()}: {response.read().decode('utf-8')}"
                xbmc.log(f"[SC3] API get_items failed: {error_msg}", xbmc.LOGERROR)
                return []
            data = response.read()
            return json.loads(data)
    except urllib.error.HTTPError as e:
        try:
            error_detail = json.loads(e.read().decode('utf-8')).get('detail', str(e))
        except json.JSONDecodeError:
            error_detail = str(e)
        xbmc.log(f"[SC3] API get_items HTTP error: {e.code} - {error_detail}", xbmc.LOGERROR)
        return []
    except Exception as e:
        xbmc.log(f"[SC3] API get_items error: {str(e)}", xbmc.LOGERROR)
        return []

def get_item(collection: str, field: str, value: Any, query=None):
    """Získaj jednu položku podľa kolekcie, poľa a voliteľného query pomocou POST"""
    try:
        url = f"{API_BASE}/item"
        post_data = json.dumps({
            "collection": collection,
            "field": field,
            "value": value,
            "query": query or {}
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=post_data, method="POST")
        req.add_header("Content-Type", "application/json")
        
        xbmc.log(f"[SC3] API get_item request: {url} with data: {post_data.decode('utf-8')}", xbmc.LOGDEBUG)
        
        with urllib.request.urlopen(req) as response:
            if response.getcode() != 200:
                error_msg = f"HTTP {response.getcode()}: {response.read().decode('utf-8')}"
                xbmc.log(f"[SC3] API get_item failed: {error_msg}", xbmc.LOGERROR)
                return None
            data = response.read()
            return json.loads(data)
    except urllib.error.HTTPError as e:
        try:
            error_detail = json.loads(e.read().decode('utf-8')).get('detail', str(e))
        except json.JSONDecodeError:
            error_detail = str(e)
        xbmc.log(f"[SC3] API get_item HTTP error: {e.code} - {error_detail}", xbmc.LOGERROR)
        return None
    except Exception as e:
        xbmc.log(f"[SC3] API get_item error: {str(e)}", xbmc.LOGERROR)
        return None

def insert_item(collection: str, data: dict):
    """Vlož novú položku pomocou POST"""
    try:
        url = f"{API_BASE}/insert_item"
        json_data = json.dumps({"collection": collection, "data": data}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=json_data, method="POST")
        req.add_header("Content-Type", "application/json")
        
        xbmc.log(f"[SC3] API insert_item request: {url} with data: {json_data.decode('utf-8')}", xbmc.LOGDEBUG)
        
        with urllib.request.urlopen(req) as response:
            if response.getcode() != 200:
                error_msg = f"HTTP {response.getcode()}: {response.read().decode('utf-8')}"
                xbmc.log(f"[SC3] API insert_item failed: {error_msg}", xbmc.LOGERROR)
                return None
            resp_data = response.read()
            return json.loads(resp_data)
    except urllib.error.HTTPError as e:
        try:
            error_detail = json.loads(e.read().decode('utf-8')).get('detail', str(e))
        except json.JSONDecodeError:
            error_detail = str(e)
        xbmc.log(f"[SC3] API insert_item HTTP error: {e.code} - {error_detail}", xbmc.LOGERROR)
        return None
    except Exception as e:
        xbmc.log(f"[SC3] API insert_item error: {str(e)}", xbmc.LOGERROR)
        return None

def update_item(collection: str, field: str, value: str, update_data: dict):
    """Aktualizuj položku podľa poľa pomocou POST"""
    try:
        url = f"{API_BASE}/update_item"
        post_data = json.dumps({
            "collection": collection,
            "field": field,
            "value": str(value),
            "update": update_data
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=post_data, method="POST")
        req.add_header("Content-Type", "application/json")
        
        xbmc.log(f"[SC3] API update_item request: {url} with data: {post_data.decode('utf-8')}", xbmc.LOGDEBUG)
        
        with urllib.request.urlopen(req) as response:
            if response.getcode() != 200:
                error_msg = f"HTTP {response.getcode()}: {response.read().decode('utf-8')}"
                xbmc.log(f"[SC3] API update_item failed: {error_msg}", xbmc.LOGERROR)
                return None
            resp_data = response.read()
            return json.loads(resp_data)
    except urllib.error.HTTPError as e:
        try:
            error_detail = json.loads(e.read().decode('utf-8')).get('detail', str(e))
        except json.JSONDecodeError:
            error_detail = str(e)
        xbmc.log(f"[SC3] API update_item HTTP error: {e.code} - {error_detail}", xbmc.LOGERROR)
        return None
    except Exception as e:
        xbmc.log(f"[SC3] API update_item error: {str(e)}", xbmc.LOGERROR)
        return None

def delete_item(collection: str, field: str, value: str):
    """Zmaž položku pomocou POST"""
    try:
        url = f"{API_BASE}/delete_item"
        post_data = json.dumps({
            "collection": collection,
            "field": field,
            "value": str(value)
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=post_data, method="POST")
        req.add_header("Content-Type", "application/json")
        
        xbmc.log(f"[SC3] API delete_item request: {url} with data: {post_data.decode('utf-8')}", xbmc.LOGDEBUG)
        
        with urllib.request.urlopen(req) as response:
            if response.getcode() != 200:
                error_msg = f"HTTP {response.getcode()}: {response.read().decode('utf-8')}"
                xbmc.log(f"[SC3] API delete_item failed: {error_msg}", xbmc.LOGERROR)
                return None
            resp_data = response.read()
            return json.loads(resp_data)
    except urllib.error.HTTPError as e:
        try:
            error_detail = json.loads(e.read().decode('utf-8')).get('detail', str(e))
        except json.JSONDecodeError:
            error_detail = str(e)
        xbmc.log(f"[SC3] API delete_item HTTP error: {e.code} - {error_detail}", xbmc.LOGERROR)
        return None
    except Exception as e:
        xbmc.log(f"[SC3] API delete_item error: {str(e)}", xbmc.LOGERROR)
        return None

def run_aggregation(collection: str, pipeline: list):
    """Spusti aggregate pipeline pomocou POST"""
    try:
        url = f"{API_BASE}/aggregate"
        post_data = json.dumps({"collection": collection, "pipeline": pipeline}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=post_data, method="POST")
        req.add_header("Content-Type", "application/json")
        
        xbmc.log(f"[SC3] API run_aggregation request: {url} with pipeline: {json.dumps(pipeline)}", xbmc.LOGDEBUG)
        
        with urllib.request.urlopen(req) as response:
            if response.getcode() != 200:
                error_msg = f"HTTP {response.getcode()}: {response.read().decode('utf-8')}"
                xbmc.log(f"[SC3] API run_aggregation failed: {error_msg}", xbmc.LOGERROR)
                return []
            resp_data = response.read()
            result = json.loads(resp_data)
            for doc in result:
                if isinstance(doc.get("_id"), dict):
                    doc["_id"] = str(doc["_id"])
            return result
    except urllib.error.HTTPError as e:
        try:
            error_detail = json.loads(e.read().decode('utf-8')).get('detail', str(e))
        except json.JSONDecodeError:
            error_detail = str(e)
        xbmc.log(f"[SC3] API run_aggregation HTTP error: {e.code} - {error_detail}", xbmc.LOGERROR)
        return []
    except Exception as e:
        xbmc.log(f"[SC3] API run_aggregation error: {str(e)}", xbmc.LOGERROR)
        return []