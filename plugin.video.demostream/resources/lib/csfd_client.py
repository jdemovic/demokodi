import re
import urllib.request

def fetch_csfd_tip_titles():
    url = "https://www.csfd.cz/televize/"
    headers = {'User-Agent': 'Mozilla/5.0'}
    req = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            html = response.read().decode("utf-8")
    except Exception as e:
        return {"error": str(e)}

    # Regex na názov filmu a rok
    pattern = r'<a href="/film/\d+-[^/]+/" class="film-title-name">([^<]+)</a>\s*<span class="film-title-info"><span class="info">\((\d{4})\)</span></span>'
    matches = re.findall(pattern, html, re.DOTALL)

    if not matches:
        return {"error": "Žiadne tipy na dnes neboli nájdené."}

    # Vráti zoznam dictov: [{"title": "Názov", "year": "2022"}, ...]
    return [{"title": title.strip(), "year": year} for title, year in matches]