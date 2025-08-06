import redis
import json
import xbmc
import xbmcgui
import xbmcaddon
from bson import ObjectId
from datetime import datetime

# ==== VLASTNÉ NASTAVENIA REDISU TU ↓↓↓ ====
#REDIS_HOST = "redis.demolator.app"
REDIS_HOST = "91.99.208.89"
REDIS_PORT = 6379
REDIS_SSL = True
REDIS_PASSWORD = None  # ak máš heslo, zadaj ako string
REDIS_TTL = 600  # v sekundách
# =========================================

class RedisCache:
    def __init__(self):
        self.ttl = REDIS_TTL
        self.client = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            decode_responses=True
        )

    def _convert_for_redis(self, data):
        """Konvertuje MongoDB špeciálne typy na serializovateľné formáty"""
        if isinstance(data, (list, tuple)):
            return [self._convert_for_redis(item) for item in data]
        elif isinstance(data, dict):
            return {key: self._convert_for_redis(value) for key, value in data.items()}
        elif isinstance(data, ObjectId):
            return str(data)
        elif isinstance(data, datetime):
            return data.isoformat()
        return data

    def get(self, key):
        try:
            value = self.client.get(key)
            return json.loads(value) if value else None
        except Exception as e:
            print(f"[RedisCache] GET error for key {key}: {e}")
            xbmc.log(f"[RedisCache] GET error for key {key}: {e}", xbmc.LOGERROR)
            return None
    
    def get_or_cache(self, key, query_fn, ttl=None):
        """Ak je cache dostupná, vráti ju. Inak zavolá query_fn, uloží výsledok a vráti ho."""
        try:
            value = self.get(key)
            if value is not None:
                xbmc.log(f"[RedisCache] Cache hit for key {key}", xbmc.LOGDEBUG)
                return value
            # Cache miss → zavolaj query
            data = query_fn()
            xbmc.log(f"[RedisCache] Cache miss for key {key}, querying...", xbmc.LOGDEBUG)
            if data:
                # Konvertuj dáta pred uložením
                serializable_data = self._convert_for_redis(data)
                self.set(key, serializable_data, ttl)
            return data
        except Exception as e:
            print(f"[RedisCache] get_or_cache error for key {key}: {e}")
            return query_fn()  # fallback aj pri chybe v cache

    def set(self, key, data, ttl=None):
        try:
            ttl = ttl or self.ttl
            self.client.setex(key, ttl, json.dumps(data))
        except Exception as e:
            xbmc.log(f"[RedisCache] SET error for key {key}: {e}", xbmc.LOGERROR)
            print(f"[RedisCache] SET error for key {key}: {e}")

    def delete(self, key):
        try:
            self.client.delete(key)
        except Exception as e:
            xbmc.log(f"[RedisCache] DELETE error for key {key}: {e}", xbmc.LOGERROR)
            print(f"[RedisCache] DELETE error for key {key}: {e}")

# Jedna globálna inštancia, ktorú môžeš importovať ako redis_cache
redis_cache = RedisCache()
