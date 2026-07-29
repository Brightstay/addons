# Versions

## 0.3.5

- Une annonce de la tablette ne détourne plus le boîtier. N'importe quel
  appareil du Wi-Fi du logement pouvait s'annoncer et se faire livrer le mot
  de passe d'administration de la tablette. L'adresse déjà connue passe
  maintenant en premier, et l'identité de la tablette est retenue : une autre
  machine qui répond à sa place est écartée.
- Les commandes reçues pendant un envoi intermédiaire ne sont plus perdues :
  elles étaient marquées « livrées » sans être exécutées, et n'étaient
  reprises qu'après le délai de re-livraison.

## 0.3.4

- La recherche de la tablette sait rendre TOUTES celles du réseau, et plus
  seulement la première. Le boîtier, lui, s'arrête toujours à la première :
  un logement n'a qu'une tablette. C'est l'atelier qui avait besoin de les
  voir toutes, pour ne pas en régler une au hasard.

## 0.3.3

- L'agent annonce la version de l'add-on installé au lieu d'un numéro écrit à
  part dans son code. Les deux avaient déjà divergé : l'add-on 0.3.2 se
  déclarait 0.3.0 dans la flotte.

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
