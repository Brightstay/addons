ARG BUILD_FROM
FROM ${BUILD_FROM}
# Python3 est déjà dans les images de base HA ; agent = stdlib pure, zéro pip.
COPY agent.py /agent.py
COPY run.sh /run.sh
RUN chmod a+x /run.sh
CMD [ "/run.sh" ]
