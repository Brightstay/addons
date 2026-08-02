#!/usr/bin/with-contenv bashio
# Point d'entrée de l'add-on.
export HA_URL="http://supervisor/core"
export HA_CONFIG_DIR="/homeassistant"
export BS_SYNC_INTERVAL="$(bashio::config 'sync_interval')"

# LE JETON DE LA TABLETTE — surtout pas celui de l'agent. Le SUPERVISOR_TOKEN
# ouvre le Superviseur ; Home Assistant, lui, le refuse. Servir l'un pour
# l'autre donnait une page qui s'affiche et ne commande rien (29/07/2026).
# `|| true` : l'option peut manquer sur un add-on installé avant elle, et
# l'agent doit démarrer quand même — il le dira dans son journal.
export BS_PAD_HA_TOKEN="$(bashio::config 'ha_token' || true)"

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
