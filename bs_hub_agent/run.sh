#!/usr/bin/with-contenv bashio
# Point d'entrée de l'add-on. Le SUPERVISOR_TOKEN est fourni par le Supervisor
# HA ; l'agent parle au core via http://supervisor/core. Les secrets de
# provisioning (URL hub-sync + clé du hub) viennent de l'image dorée.
export HA_URL="http://supervisor/core"
export HA_CONFIG_DIR="/homeassistant"
export BS_SYNC_INTERVAL="$(bashio::config 'sync_interval')"
exec python3 /agent.py
