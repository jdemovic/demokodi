import os
import zipfile
import hashlib
from xml.etree import ElementTree as ET

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
ZIPS_DIR = os.path.join(REPO_DIR, "zips")
ADDONS_XML = os.path.join(REPO_DIR, "addons.xml")
ADDONS_MD5 = os.path.join(REPO_DIR, "addons.xml.md5")

def get_addon_dirs():
    return [d for d in os.listdir(REPO_DIR) if os.path.isdir(d) and d.startswith(("plugin.", "repository."))]

def create_zip(addon_dir):
    addon_xml = os.path.join(REPO_DIR, addon_dir, "addon.xml")
    tree = ET.parse(addon_xml)
    addon_id = tree.getroot().attrib["id"]
    addon_version = tree.getroot().attrib["version"]

    dest_dir = os.path.join(ZIPS_DIR, addon_id)
    os.makedirs(dest_dir, exist_ok=True)
    zip_path = os.path.join(dest_dir, f"{addon_id}-{addon_version}.zip")

    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(os.path.join(REPO_DIR, addon_dir)):
            for file in files:
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, REPO_DIR)
                zf.write(full_path, rel_path)

    return addon_id, tree

def write_addons_xml(addon_elements):
    root = ET.Element("addons")
    for element in addon_elements:
        root.append(element.getroot())

    tree = ET.ElementTree(root)
    tree.write(ADDONS_XML, encoding="UTF-8", xml_declaration=True)

def write_md5():
    with open(ADDONS_XML, "rb") as f:
        content = f.read()
    md5 = hashlib.md5(content).hexdigest()
    with open(ADDONS_MD5, "w") as f:
        f.write(md5)

if __name__ == "__main__":
    os.makedirs(ZIPS_DIR, exist_ok=True)
    addon_elements = []

    for addon in get_addon_dirs():
        print(f"Packing {addon}...")
        addon_id, tree = create_zip(addon)
        addon_elements.append(tree)

    write_addons_xml(addon_elements)
    write_md5()
    print("Done.")