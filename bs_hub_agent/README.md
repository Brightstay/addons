# Brightstay Hub Agent

Relie le logement à Brightstay.

Il appelle nos serveurs toutes les cinq minutes — jamais l'inverse, donc rien à
configurer sur la box internet. Il remonte ce que voit le logement, applique ce
qu'on lui demande, et sert la page de la tablette depuis le boîtier, y compris
quand internet est coupé.

Ni caméra ni micro : ce qui circule, ce sont des mesures et des ordres.

## Réglage

| | Défaut | |
|---|---|---|
| `sync_interval` | 300 | Secondes entre deux appels. Il enchaîne sans attendre quand il a du travail. |

Les accès du boîtier sont posés à l'atelier, dans la machine. Ce sont des
secrets, pas des réglages.

## Installation

Faite en atelier avant l'expédition. Rien à faire.

contact@brightstay.fr
