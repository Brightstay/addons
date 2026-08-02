ARG BUILD_FROM
FROM ${BUILD_FROM}

# L'agent n'utilise que la bibliothèque standard de Python — aucune dépendance
# à installer, donc rien qui puisse casser parce qu'une bibliothèque a bougé.
# Mais Python lui-même n'est pas dans l'image de base : on l'ajoute.
RUN apk add --no-cache python3

COPY agent.py /agent.py
# ⚠️ LE SOCLE VOYAGE DANS L'IMAGE, et cette ligne est ce qui le garantit.
# Sans elle, l'agent cherche `/socle` et ne trouve rien : le boîtier sert 404
# alors que tous les contrôles passent — ils tournent depuis le dépôt, où le
# dossier existe. C'est le filet du parc entier qui tient à ce COPY.
COPY socle /socle
COPY run.sh /run.sh
RUN chmod a+x /run.sh
CMD [ "/run.sh" ]
