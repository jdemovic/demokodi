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
        self.salt = None
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

    def get_stream_url(self, ident, retry=True, mongo_collection=None):
        if not self.token and not self.login():
            xbmcgui.Dialog().notification("Webshare prihlásenie zlyhalo", "", xbmcgui.NOTIFICATION_ERROR)
            return None

        try:
            url = self.url + "file_link/"
            payload = {"ident": ident, "wst": self.token}

            # ➕ Získaj heslo z MongoDB, ak kolekcia bola poskytnutá
            if mongo_collection is not None:
                doc = mongo_collection.find_one({"ident": ident})
                if doc and "pass" in doc and doc["pass"]:
                    # Ziskaj salt pre ident z webshare API /file_password_salt
                    xbmc.log(f"🔑 Načítavam heslo pre ident {ident} z webshare API.")
                    
                    file_salt_payload = {
                        "ident": ident
                    }

                    file_salt_data = urllib.parse.urlencode(file_salt_payload).encode("utf-8")
                    file_salt_req = urllib.request.Request(f"{self.url}file_password_salt/", data=file_salt_data, headers=HEADERS)
                    
                    # Ak sa podarí získať salt, použijeme ho na šifrovanie hesla
                    with urllib.request.urlopen(file_salt_req) as response:
                        file_salt_response = response.read().decode("utf-8")

                    file_salt_dict = parse_xml_response(file_salt_response)
                    if file_salt_dict.get("status") != "OK":
                        xbmc.log("❌ Ziskanie saltu pre ident zlyhalo", xbmc.LOGERROR)
                        return None

                    self.fileSalt = file_salt_dict["salt"]
                    xbmc.log(f"🔑 Salt pre ident {ident} získaný: {self.fileSalt}")
                    
                    # Šifruj heslo pomocou získaného salt a password z mongoDB
                    ident_pass = hashlib.sha1(
                        md5crypt(doc["pass"].encode('utf-8'), self.fileSalt.encode('utf-8')).encode('utf-8')
                    ).hexdigest()
                    
                    payload["password"] = ident_pass

            data = urllib.parse.urlencode(payload).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers=HEADERS)

            with urllib.request.urlopen(req) as response:
                response_text = response.read().decode("utf-8")
                link_dict = parse_xml_response(response_text)

                if link_dict.get("status") == "OK":
                    return link_dict.get("link")
                elif retry:
                    self.token = None
                    return self.get_stream_url(ident, retry=False, mongo_collection=mongo_collection)
                else:
                    code = link_dict.get("code")

                    if code == "FILE_LINK_FATAL_3":
                        xbmcgui.Dialog().notification("Súbor je zaheslovaný", "Nie je možné ho prehrať.", xbmcgui.NOTIFICATION_ERROR)
                        return "password_protected"

                    elif code == "FILE_LINK_FATAL_4":
                        xbmcgui.Dialog().notification("Dočasne nedostupné", "Súbor je momentálne nedostupný.", xbmcgui.NOTIFICATION_WARNING)
                        return "temporarily_unavailable"

                    elif code == "FILE_LINK_FATAL_6":
                        xbmcgui.Dialog().notification("Súkromný súbor", "Obsah nie je verejný. Môže ísť o autorsky chránené video.", xbmcgui.NOTIFICATION_ERROR)
                        return "non_public_file"

                    elif code == "FILE_LINK_FATAL_1":
                        user_choice = xbmcgui.Dialog().yesno(
                            "Súbor nenájdený",
                            f"Súbor s ident {ident} neexistuje.\nChceš ho vymazať z databázy?"
                        )
                        if user_choice:
                            if mongo_collection is not None:
                                try:
                                    result = mongo_collection.delete_one({"ident": ident})
                                    if result.deleted_count > 0:
                                        xbmc.log(f"🗑️ Dokument s ident {ident} vymazaný z kolekcie.", xbmc.LOGINFO)
                                        return "deleted"
                                    else:
                                        xbmc.log(f"⚠️ Dokument s ident {ident} sa nenašiel na vymazanie.", xbmc.LOGWARNING)
                                        return "not_found"
                                except Exception as e:
                                    error_message = str(e)
                                    if "not authorized" in error_message or "Unauthorized" in error_message:
                                        xbmc.log("🚫 Používateľ nemá práva na mazanie z MongoDB.", xbmc.LOGERROR)
                                        return "unauthorized"
                                    else:
                                        xbmc.log(f"❌ Výnimka pri mazaní dokumentu: {error_message}", xbmc.LOGERROR)
                                        return "delete_error"
                        else:
                            return "cancel"

                    xbmc.log(f"❌ Neúspešný response aj po retry: {link_dict}", xbmc.LOGERROR)
                    return None

        except Exception as e:
            xbmc.log(f"❌ Výnimka pri načítaní stream URL: {e}", xbmc.LOGERROR)
            return None


#-------- XML parsovanie --------
def parse_xml_response(text):
    try:
        root = ET.fromstring(text)
        return {child.tag: child.text for child in root}
    except Exception as e:
        xbmc.log(f"❌ Chyba pri XML parsovaní: {e}", xbmc.LOGERROR)
        return {}
