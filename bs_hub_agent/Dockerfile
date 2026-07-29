ARG BUILD_FROM
FROM ${BUILD_FROM}

# L'agent n'utilise que la bibliothèque standard de Python — aucune dépendance
# à installer, donc rien qui puisse casser parce qu'une bibliothèque a bougé.
# Mais Python lui-même n'est pas dans l'image de base : on l'ajoute.
RUN apk add --no-cache python3

COPY agent.py /agent.py
COPY run.sh /run.sh
RUN chmod a+x /run.sh
CMD [ "/run.sh" ]
