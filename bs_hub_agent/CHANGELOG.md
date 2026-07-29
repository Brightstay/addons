# Historique des versions

Home Assistant affiche ce fichier au moment de proposer une mise à jour.
Écrire ici ce qui change, en clair : c'est ce que lira la personne qui décide
de mettre à jour, pas nous.

## 0.3.0

- Le boîtier sait répondre à « liste-moi les appareils du logement ». C'est ce
  qui permet à l'hôte de ranger ses lampes et ses volets depuis son espace
  Brightstay, sans jamais ouvrir Home Assistant.
- Il refuse proprement de faire cet inventaire tant que Home Assistant n'a pas
  fini de démarrer : une liste incomplète remplacerait la bonne.
- Il dit désormais l'empreinte de sa machine à chaque appel. Elle change si le
  boîtier est réinstallé — ce qui permet de repérer un boîtier refait, ou une
  clé recopiée sur une autre machine.

## 0.2.0

- Accès au Superviseur : le boîtier peut se mettre à jour lui-même, mettre à
  jour Home Assistant et prendre une sauvegarde. Avant, le moindre correctif
  imposait un déplacement chez chaque hôte.
- Le boîtier sert lui-même la page de la tablette, en HTTPS, depuis le réseau
  local. Le logement devient autonome : la tablette démarre même quand la box
  internet est coupée.
