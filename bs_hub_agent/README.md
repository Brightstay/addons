# Brightstay Hub Agent

Le programme qui relie ce logement à Brightstay.

## Ce qu'il fait

**Il téléphone, on ne l'appelle jamais.** Toutes les cinq minutes, il appelle
nos serveurs : voilà ce que je vois chez moi, avez-vous quelque chose pour moi ?
C'est ce sens-là qui compte : un appel qui **part** de la maison traverse
n'importe quelle box internet sans rien configurer, sans ouvrir de porte, sans
adresse fixe.

Concrètement, trois choses :

- **il rapporte des faits** — température, ouvertures, détecteurs, état des
  appareils, et sa propre santé ;
- **il exécute** ce qu'on lui demande : installer une consigne, se mettre à
  jour, prendre une sauvegarde, faire réagir un appareil ;
- **il sert la page de la tablette** au mur, depuis le boîtier lui-même. C'est
  ce qui fait que le logement continue de fonctionner quand internet tombe.

## Ce qu'il ne fait pas

Il ne transporte **ni image ni son** : il n'y a ni caméra ni micro dans le kit.
Ce qui circule, ce sont des mesures et des ordres.

Il ne décide de rien tout seul. Les règles vivent côté serveur ; lui applique.

## Réglage

| Réglage | Défaut | À quoi ça sert |
|---|---|---|
| `sync_interval` | 300 | Secondes entre deux appels. Quand il a du travail, il enchaîne sans attendre. |

Les accès du boîtier (adresse du serveur, clé du logement) sont posés à
l'atelier, dans la machine. Ils n'apparaissent pas ici et ne sont pas
modifiables depuis l'écran : ce sont des secrets, pas des réglages.

## Installation

Cet add-on est installé en atelier, avant l'expédition. Il n'y a normalement
rien à faire.

## Support

contact@brightstay.fr
