ARG BUILD_FROM
FROM ${BUILD_FROM}

# Python n'est pas dans l'image de base : on l'ajoute.
#
# ⚠️ UNE SEULE DÉPENDANCE, ET ELLE EST LÀ POUR UNE RAISON DE SÉCURITÉ.
# L'agent tenait tout entier sur la bibliothèque standard, ce qui était une
# qualité. Mais il doit juger le CONTENU des fichiers de configuration qu'il
# écrit, et juger un YAML suppose de le lire vraiment. `py3-yaml` est la
# bibliothèque qu'utilise Home Assistant lui-même : même lecteur, même verdict.
RUN apk add --no-cache python3 py3-yaml

COPY agent.py /agent.py
# ⚠️ LE SOCLE VOYAGE DANS L'IMAGE, et cette ligne est ce qui le garantit.
# Sans elle, l'agent cherche `/socle` et ne trouve rien : le boîtier sert 404
# alors que tous les contrôles passent — ils tournent depuis le dépôt, où le
# dossier existe. C'est le filet du parc entier qui tient à ce COPY.
COPY socle /socle
COPY run.sh /run.sh
RUN chmod a+x /run.sh
CMD [ "/run.sh" ]
