## 0.5.1 — 2026-08-02

- À DÉCRIRE : ce que cette version change pour un hôte.

## 0.5.0 — 2026-08-02

**Le voyageur a toujours un écran, même le premier jour.**

- Le boîtier emporte désormais l'écran Brightstay avec lui (logo et police
  compris). Avant, un boîtier fraîchement installé servait une page d'erreur
  jusqu'à ce qu'un paquet soit publié, déployé ET reçu — trois maillons, chacun
  capable de manquer. Un filet qu'il faut télécharger n'est pas un filet.
- L'écran du logement se met à jour par morceaux : corriger une ligne de la page
  ne fait plus retélécharger les 14 Mo d'illustrations. Une expérience sur mesure
  se pose par-dessus sans remplacer le reste, et ce qu'elle ne fournit pas
  retombe sur la couche du dessous.
- ⛔ Rien venu du serveur ne peut effacer cet écran de secours : un ordre de
  déploiement qui le viserait est refusé. C'est le dernier recours du parc.
- Les paquets sur mesure se téléchargent maintenant par une adresse signée,
  valable une heure : ils ne sont plus lisibles par qui connaît leur nom.

# Versions

## 0.4.1

- La tablette reçoit désormais de quoi joindre Home Assistant même quand le
  boîtier est installé derrière un réseau interne. Un nouveau réglage,
  « ha_token », reçoit le jeton d'accès créé pour elle à l'atelier.
- Le boîtier n'annonce plus d'accès qu'il sait inutilisables : il préfère dire
  « non connecté » plutôt que laisser la tablette essayer sans fin, et il
  l'écrit dans son journal au démarrage.

## 0.4.0

- L'écran de la tablette n'est plus un bloc de 14 Mo mais des couches servies
  en cascade : l'habillage d'un client, les illustrations de référence, la
  page. Le premier qui a le fichier gagne. Corriger la page ne fait plus
  descendre que 440 Ko, et un habillage qui ne redessine que douze appareils
  n'a aucun trou — le reste vient de la couche du dessous.
- On revient en arrière couche par couche : défaire une mauvaise page ne
  retélécharge pas 40 Mo d'illustrations qui n'y sont pour rien.
- Rien à faire sur un boîtier déjà installé : l'ancien paquet unique reste
  servi, en dernier recours, et les couches le remplacent l'une après l'autre.

## 0.3.6

- La mise à jour de l'agent fonctionne enfin. Elle envoyait une version au
  Superviseur, qui refuse d'en recevoir une : la commande échouait à tous les
  coups. Elle demande maintenant simplement « prends ce que la boutique
  propose », après avoir fait relire cette boutique.
- Et elle REFUSE de le faire si la boutique ne propose pas la version que la
  fiche du logement demande : un boîtier ne prend pas une nouveauté qui ne lui
  était pas destinée.

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
