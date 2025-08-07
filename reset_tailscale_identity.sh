#!/bin/bash

echo "================================================================="
echo "Tento skript resetuje Tailscale identitu tohoto zařízení."
echo "Spusťte ho POUZE na novém, naklonovaném zařízení."
echo "================================================================="
echo

# Žádost o potvrzení, aby se předešlo nechtěnému spuštění
read -p "Opravdu chcete pokračovat a vygenerovat novou identitu? (ano/ne): " a
if [[ ! $a =~ ^[aA][nN][oO]$ ]]; then
    echo "Akce zrušena."
    exit 1
fi

echo
echo "Zastavuji Tailscale službu..."
sudo systemctl stop tailscaled.service

echo "Mažu starou, naklonovanou identitu..."
sudo rm -f /var/lib/tailscale/tailscaled.state

echo "Restartuji Tailscale službu..."
sudo systemctl restart tailscaled.service

echo
echo "Spouštím novou registraci zařízení."
echo "Prosím, otevřete následující URL v prohlížeči a autorizujte zařízení:"
sudo tailscale up --ssh

echo
echo "Proces dokončen. Po úspěšné autorizaci doporučuji toto zařízení restartovat."
