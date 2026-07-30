#!/usr/bin/with-contenv bashio
export HA_URL="http://supervisor/core"
export HA_CONFIG_DIR="/homeassistant"
export BS_SYNC_INTERVAL="$(bashio::config 'sync_interval')"

export BS_PAD_HA_TOKEN="$(bashio::config 'ha_token' || true)"

if [ -z "${BS_HUB_SYNC_URL:-}" ] && bashio::config.has_value 'hub_sync_url'; then
    export BS_HUB_SYNC_URL="$(bashio::config 'hub_sync_url')"
fi
if [ -z "${BS_HUB_KEY:-}" ] && bashio::config.has_value 'hub_key'; then
    export BS_HUB_KEY="$(bashio::config 'hub_key')"
fi

if [ -z "${BS_HUB_SYNC_URL:-}" ] || [ -z "${BS_HUB_KEY:-}" ]; then
    bashio::log.fatal "Adresse du serveur ou clé du logement manquante."
    bashio::log.fatal "Renseignez-les dans l'onglet Configuration de cet add-on."
    bashio::exit.nok
fi

exec python3 /agent.py
