# Versions

## 0.3.2

- L'add-on se fabrique enfin : il manquait le fichier qui dit sur quelle base
  le construire, et Python n'était pas installé dessus.
- Icône et logo Brightstay dans la boutique.
- Retrait des vieilles machines 32 bits (armv7), que Home Assistant ne
  reconnaît plus.

## 0.3.1

- L'adresse du serveur et la clé du logement peuvent être saisies dans l'écran
  de l'add-on. Elles venaient uniquement de l'image préparée en atelier :
  impossible d'installer l'agent sur un Home Assistant ordinaire.
- Si elles manquent, l'add-on le dit en clair au lieu de planter.

## 0.3.0

- Répond à la demande d'inventaire : l'hôte range ses appareils depuis son
  espace Brightstay, sans ouvrir Home Assistant.
- Refuse cet inventaire tant que Home Assistant n'a pas fini de démarrer — une
  liste incomplète remplacerait la bonne.
- Rapporte l'empreinte de sa machine, qui change s'il est réinstallé.

## 0.2.0

- Se met à jour lui-même, met à jour Home Assistant, prend des sauvegardes.
  Avant, le moindre correctif imposait un déplacement.
- Sert la page de la tablette en HTTPS depuis le réseau local : elle démarre
  même quand la box internet est coupée.
