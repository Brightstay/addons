#!/usr/bin/with-contenv bashio
# Point d'entrée de l'add-on.
export HA_URL="http://supervisor/core"
export HA_CONFIG_DIR="/homeassistant"
export BS_SYNC_INTERVAL="$(bashio::config 'sync_interval')"

# L'adresse du serveur et la clé du logement viennent normalement de l'image
# préparée en atelier, qui les pose dans l'environnement. Sur une installation
# ordinaire — un boîtier d'essai, ou un hôte qui a déjà son Home Assistant —
# rien ne les pose : on les prend alors dans les réglages de l'add-on.
#
# L'environnement gagne toujours : un kit préparé en atelier ne doit pas
# pouvoir être détourné vers un autre serveur depuis l'écran.
if [ -z "${BS_HUB_SYNC_URL:-}" ] && bashio::config.has_value 'hub_sync_url'; then
    export BS_HUB_SYNC_URL="$(bashio::config 'hub_sync_url')"
fi
if [ -z "${BS_HUB_KEY:-}" ] && bashio::config.has_value 'hub_key'; then
    export BS_HUB_KEY="$(bashio::config 'hub_key')"
fi

# Sans ces deux valeurs, l'agent ne sait ni qui appeler ni au nom de qui. On le
# dit en clair plutôt que de laisser planter sur une erreur incompréhensible.
if [ -z "${BS_HUB_SYNC_URL:-}" ] || [ -z "${BS_HUB_KEY:-}" ]; then
    bashio::log.fatal "Adresse du serveur ou clé du logement manquante."
    bashio::log.fatal "Renseignez-les dans l'onglet Configuration de cet add-on."
    bashio::exit.nok
fi

exec python3 /agent.py
