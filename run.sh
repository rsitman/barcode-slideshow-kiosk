#!/bin/bash
# Přejdeme do adresáře, kde je skript
cd "$(dirname "$0")"

# Přesměrování veškerého logování do souboru
exec &> /home/admin/barcode_slideshow/full_app.log

echo "Autostart skript spuštěn (X11 mode)."
echo "----------------------------------------------------"

# Spuštění unclutter pro skrytí kurzoru na X11
unclutter -idle 1 -root &
echo "Unclutter spuštěn na pozadí."

# Hlavní Python aplikace
echo "Spouštím hlavní Python aplikaci..."
.venv/bin/python main.py

# Úklid
echo "Hlavní aplikace ukončena, zabíjím unclutter."
killall unclutter
