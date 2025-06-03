# demokodi

**demokodi** je osobný repozitár pre doplnky (pluginy) pre multimediálne centrum [Kodi](https://kodi.tv/).  
Slúži ako zdroj pre inštaláciu a aktualizáciu vlastných doplnkov cez Kodi.

## Obsah repozitára

- `plugin.video.demostream` – doplnok pre streamovanie videa
- `repository.demolator` – repozitár pre distribúciu doplnkov
- `zips/` – predpripravené ZIP archívy doplnkov
- `addons.xml`, `addons.xml.md5` – súbory pre správu metadát repozitára
- `create_repository.py`, `generate_addons.py` – skripty na generovanie a aktualizáciu repozitára
- `index.html` – jednoduchý webový index pre prehliadanie repozitára
- `rename_folders.ps1` – PowerShell skript na premenovanie priečinkov

## Inštalácia v Kodi

### Možnosť 1: Inštalácia zo ZIP súboru

1. Stiahni súbor `repository.demolator-1.0.0.zip` zo zložky `zips/`.
2. V Kodi prejdite do **Doplnky > Inštalovať zo ZIP súboru**.
3. Vyber stiahnutý ZIP súbor.
4. Po inštalácii repozitára môžeš nainštalovať doplnky z **Inštalovať z repozitára > Demolator Repository**.

### Možnosť 2: Inštalácia cez URL zdroj v Správcovi súborov

1. Otvor Kodi a prejdite do **Nastavenia > Správca súborov**.
2. Zvoľ **Pridať zdroj** a ako cestu zadaj: https://repo.demolator.app/
3. Pomenuj zdroj napríklad `demokodi` a potvrď.
4. Vráť sa späť do **Doplnky > Inštalovať zo ZIP súboru** a vyber pridaný zdroj `demokodi`.
5. Nainštaluj súbor `repository.demolator-1.0.0.zip`.
6. Po inštalácii repozitára môžeš inštalovať doplnky cez **Inštalovať z repozitára**.

## Požiadavky

- Kodi 19 alebo novší
- Prístup na internet pre sťahovanie a aktualizáciu doplnkov

## Vývoj a úpravy

Skript `generate_addons.py` slúži na generovanie súborov `addons.xml` a `addons.xml.md5` na základe obsahu repozitára.
Skript `create_repository.py` pomáha s vytváraním štruktúry repozitára a balením doplnkov.

## Licencia

Tento projekt je dostupný pod licenciou MIT.  
Viac informácií nájdeš v súbore `LICENSE`.

## Kontakt

Pre viac informácií alebo návrhy môžeš kontaktovať autora cez GitHub profil [@jdemovic](https://github.com/jdemovic).
