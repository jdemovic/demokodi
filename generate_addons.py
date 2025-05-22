import os
import hashlib

def generate_addons_xml(addons_dir):
    addons_xml = "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<addons>\n"

    for addon in sorted(os.listdir(addons_dir)):
        addon_path = os.path.join(addons_dir, addon)
        addon_xml_path = os.path.join(addon_path, "addon.xml")
        if os.path.isdir(addon_path) and os.path.isfile(addon_xml_path):
            with open(addon_xml_path, 'r', encoding='utf-8') as f:
                content = f.read()
                content = content.strip()
                addons_xml += content + "\n"

    addons_xml += "</addons>"

    with open("addons.xml", "w", encoding="utf-8") as f:
        f.write(addons_xml)

    md5_hash = hashlib.md5(addons_xml.encode('utf-8')).hexdigest()
    with open("addons.xml.md5", "w") as f:
        f.write(md5_hash)

generate_addons_xml(".")