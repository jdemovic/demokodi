import redis
import json
import xbmc
import xbmcgui
import xbmcaddon
import time
from bson import ObjectId
from datetime import datetime

# ==== NASTAVENIA REDIS ====
PRIMARY_HOST = "redis.demolator.app"
FALLBACK_HOST = "redis.demostream.stream"
REDIS_PORT = 6379
REDIS_SSL = True
REDIS_PASSWORD = None  # ak máš heslo, zadaj ako string
REDIS_TTL = 600  # v sekundách
CONNECTION_CHECK_INTERVAL = 3600  # 1 hodina v sekundách
# ==========================

class RedisCache:
    def __init__(self):
        self.ttl = REDIS_TTL
        self.current_host = PRIMARY_HOST
        self.last_connection_check = 0
        self._init_client()
        self.notified_fallback = False
        self.notified_primary = False

    def _init_client(self):
        """Inicializuje Redis klienta s kontrolou dostupnosti"""
        addon = xbmcaddon.Addon()
        use_fallback_setting = addon.getSettingBool("use_fallback_server")
        
        # Manuálne prepnutie cez nastavenia
        if use_fallback_setting:
            self._switch_to_fallback(manual=True)
            return
        
        # Automatická kontrola len ak je čas
        current_time = time.time()
        if current_time - self.last_connection_check > CONNECTION_CHECK_INTERVAL:
            self._check_and_switch()
            self.last_connection_check = current_time

    def _check_and_switch(self):
        """Skontroluje pripojenie a prepne server podľa potreby"""
        try:
            # Test primárneho servera
            temp_client = redis.Redis(
                host=PRIMARY_HOST,
                port=REDIS_PORT,
                socket_timeout=3,  # kratky timeout pre test
                socket_connect_timeout=3
            )
            if temp_client.ping():
                if self.current_host != PRIMARY_HOST:
                    self._switch_to_primary()
                return
        except Exception as e:
            xbmc.log(f"[RedisCache] Primary Redis check failed: {str(e)}", xbmc.LOGDEBUG)

        # Ak primárny zlyhal, skús záložný
        try:
            temp_client = redis.Redis(
                host=FALLBACK_HOST,
                port=REDIS_PORT,
                socket_timeout=3,
                socket_connect_timeout=3
            )
            if temp_client.ping():
                if self.current_host != FALLBACK_HOST:
                    self._switch_to_fallback(manual=False)
            else:
                xbmc.log("[RedisCache] Both primary and fallback Redis servers unavailable", xbmc.LOGERROR)
        except Exception as e:
            xbmc.log(f"[RedisCache] Fallback Redis check failed: {str(e)}", xbmc.LOGERROR)

    def _switch_to_primary(self):
        """Prepne na primárny server"""
        self.current_host = PRIMARY_HOST
        self.client = redis.Redis(
            host=self.current_host,
            port=REDIS_PORT,
            decode_responses=True
        )
        xbmc.log("[RedisCache] Switched to primary Redis server", xbmc.LOGINFO)
        if not self.notified_primary:
            xbmcgui.Dialog().notification(
                "Redis Cache",
                "Pripojené k primárnemu serveru",
                xbmcgui.NOTIFICATION_INFO,
                3000
            )
            self.notified_primary = True
            self.notified_fallback = False

    def _switch_to_fallback(self, manual=False):
        """Prepne na záložný server"""
        self.current_host = FALLBACK_HOST
        self.client = redis.Redis(
            host=self.current_host,
            port=REDIS_PORT,
            decode_responses=True
        )
        
        if manual:
            xbmc.log("[RedisCache] Using fallback Redis server (manual setting)", xbmc.LOGINFO)
            xbmcgui.Dialog().notification(
                "Redis Cache",
                "Používam záložný server (manuálne nastavenie)",
                xbmcgui.NOTIFICATION_WARNING,
                3000
            )
        else:
            xbmc.log("[RedisCache] Switched to fallback Redis server", xbmc.LOGWARNING)
            if not self.notified_fallback:
                xbmcgui.Dialog().notification(
                    "Redis Cache",
                    "Primárny server nedostupný, používam záložný",
                    xbmcgui.NOTIFICATION_WARNING,
                    3000
                )
                self.notified_fallback = True
                self.notified_primary = False

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
                serializable_data = self._convert_for_redis(data)
                self.set(key, serializable_data, ttl)
            return data
        except Exception as e:
            xbmc.log(f"[RedisCache] get_or_cache error for key {key}: {e}", xbmc.LOGERROR)
            return query_fn()  # fallback aj pri chybe v cache

    def set(self, key, data, ttl=None):
        try:
            ttl = ttl or self.ttl
            self.client.setex(key, ttl, json.dumps(data))
        except Exception as e:
            xbmc.log(f"[RedisCache] SET error for key {key}: {e}", xbmc.LOGERROR)

    def delete(self, key):
        try:
            self.client.delete(key)
        except Exception as e:
            xbmc.log(f"[RedisCache] DELETE error for key {key}: {e}", xbmc.LOGERROR)

# Jedna globálna inštancia, ktorú môžeš importovať ako redis_cache
redis_cache = RedisCache()