import xbmc
import xbmcgui
import xbmcaddon
import urllib.request
import urllib.parse
import hashlib
import sys
import os

# Získaj cestu k hlavnému adresáru addon-u
addon = xbmcaddon.Addon()
addon_path = addon.getAddonInfo("path")  # cesta, ak potrebuješ
sys.path.append(addon_path)
from md5crypt import md5crypt

# Kodi používa len základné knižnice, xml parsovanie urobíme bez xmltodict
import xml.etree.ElementTree as ET

HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "text/xml"
}
  
class WebshareClient:
    def __init__(self):
        self.token = None
        self.user = addon.getSetting("webshare_user")
        self.passwd = addon.getSetting("webshare_pass")
        self.url = addon.getSetting("webshare_url")
        self.realm = ":Webshare:"

    def login(self):
        # už máš token? netreba login
        if self.token:
            xbmc.log("✅ Už si prihlásený, token je dostupný", xbmc.LOGDEBUG)
            return True

        try:
            salt_payload = urllib.parse.urlencode({"username_or_email": self.user}).encode("utf-8")
            salt_req = urllib.request.Request(f"{self.url}salt/", data=salt_payload, headers=HEADERS)
            with urllib.request.urlopen(salt_req) as response:
                salt_response = response.read().decode("utf-8")
            salt = parse_xml_response(salt_response).get("salt")

            if not salt:
                xbmc.log("❌ Salt nezískaný", xbmc.LOGERROR)
                return False

            encrypted_pass = hashlib.sha1(
                md5crypt(self.passwd.encode('utf-8'), salt.encode('utf-8')).encode('utf-8')
            ).hexdigest()
            pass_digest = hashlib.md5((self.user + self.realm + self.passwd).encode('utf-8')).hexdigest()

            login_payload = {
                "username_or_email": self.user,
                "password": encrypted_pass,
                "digest": pass_digest
            }

            login_data = urllib.parse.urlencode(login_payload).encode("utf-8")
            login_req = urllib.request.Request(f"{self.url}login/", data=login_data, headers=HEADERS)

            with urllib.request.urlopen(login_req) as response:
                login_response = response.read().decode("utf-8")

            login_dict = parse_xml_response(login_response)
            if login_dict.get("status") != "OK" or not login_dict.get("token"):
                xbmc.log("❌ Webshare login zlyhal", xbmc.LOGERROR)
                return False

            self.token = login_dict["token"]
            return True

        except Exception as e:
            xbmc.log(f"❌ Výnimka pri logine: {e}", xbmc.LOGERROR)
            return False

    def get_stream_url(self, ident, retry=True):
        if not self.token and not self.login():
            xbmc.log("❌ Prihlásenie zlyhalo, nemôžem získať stream URL", xbmc.LOGERROR)
            xbmcgui.Dialog().notification("Webshare prihlásenie zlyhalo, skontroluj nastavenia.", xbmcgui.NOTIFICATION_ERROR)
            return None

        try:
            url = self.url + "file_link/"
            payload = {"ident": ident, "wst": self.token}
            data = urllib.parse.urlencode(payload).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers=HEADERS)

            with urllib.request.urlopen(req) as response:
                response_text = response.read().decode("utf-8")
                link_dict = parse_xml_response(response_text)

                if link_dict.get("status") == "OK":
                    xbmc.log(f"✅ Stream URL pre ident {ident} úspešne získaný", xbmc.LOGDEBUG)
                    return link_dict.get("link")
                elif retry:
                    xbmc.log(f"🔁 Skúsime obnoviť token pre ident {ident}", xbmc.LOGINFO)
                    self.token = None
                    return self.get_stream_url(ident, retry=False)
                else:
                    xbmc.log(f"❌ Neúspešný response aj po re-login: {link_dict}", xbmc.LOGERROR)
                    return None

        except Exception as e:
            xbmc.log(f"❌ Chyba pri načítaní stream URL: {e}", xbmc.LOGERROR)
            return None

#-------- XML parsovanie --------
def parse_xml_response(text):
    try:
        root = ET.fromstring(text)
        return {child.tag: child.text for child in root}
    except Exception as e:
        xbmc.log(f"❌ Chyba pri XML parsovaní: {e}", xbmc.LOGERROR)
        return {}
