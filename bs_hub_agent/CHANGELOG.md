## 0.5.15 — 2026-08-03

- À DÉCRIRE : ce que cette version change pour un hôte.

## 0.5.14 — 2026-08-03

- À DÉCRIRE : ce que cette version change pour un hôte.

## 0.5.12 — 2026-08-03

- À DÉCRIRE : ce que cette version change pour un hôte.

## 0.5.11 — 2026-08-03

**L écran de la tablette ne se fige plus après une mise à jour.**

- Le mot de passe qui relie la tablette au boîtier était tiré à neuf à chaque
  redémarrage de l agent. La page du salon garde celui qu elle a lu au
  chargement : après une mise à jour, elle restait bloquée sur son écran de
  chargement, sans erreur ni alerte — et la flotte affichait « en ligne ».
  Chaque mise à jour aurait éteint l écran de tous les logements.
- Il est maintenant conservé sur le boîtier, lisible par lui seul.
- Le relais abandonne plus vite une porte qui ne répond pas : quelques
  centaines de millisecondes au lieu de vingt secondes d écran blanc.

## 0.5.10 — 2026-08-02

Le relais cherche Home Assistant par les quatre portes possibles au lieu d'en
supposer une. L'agent vit dans un conteneur isolé : il ne joint pas la machine
par son adresse de réseau local, et sa porte de service n'a pas le même chemin.

## 0.5.9 — 2026-08-02

Le relais joignait Home Assistant par la porte interne du module, où son
adresse n'existe pas : la tablette recevait une connexion coupée, sans
explication. Il passe maintenant par l'adresse réelle de la machine.

## 0.5.8 — 2026-08-02

Rien de neuf pour un hôte : la 0.5.7 a été publiée avec huit contrôles au
rouge — des contrôles qui affirmaient l'ancien fonctionnement, celui où la clé
partait dans la tablette. Ils disent maintenant la bonne chose, et l'un d'eux
vérifie explicitement que cette clé n'apparaît nulle part.

## 0.5.7 — 2026-08-02

**La clé de la domotique ne quitte plus le boîtier.**

- Jusqu'ici, le boîtier livrait à la tablette une clé d'administrateur Home
  Assistant valable dix ans, sur un port ouvert à tout le réseau du logement.
  N'importe qui ayant le mot de passe du Wi-Fi — un voyageur, son invité, un
  voisin qui l'a eu une fois — pouvait la lire et piloter la domotique : ouvrir
  ce qui s'ouvre, lire l'historique de présence, éteindre les détecteurs.
- La tablette ne parle plus à Home Assistant : elle parle au boîtier, qui
  relaie. La clé reste sur la machine. Ce que la tablette reçoit désormais
  n'ouvre que ce boîtier-là, et se renouvelle à chaque démarrage.

## 0.5.6 — 2026-08-02

**Le voyageur ne peut plus éteindre le boîtier depuis son canapé.**

- Home Assistant crée un interrupteur pour chaque module installé, dont le
  nôtre. Aucun ne portait de marque particulière : ils se seraient affichés sur
  la tablette murale à côté des lampes. Un voyageur qui coupe « Brightstay Hub
  Agent » coupait la surveillance, les mises à jour et toute réparation à
  distance — sans mauvaise intention.
- Ils sont maintenant écartés partout : dans l'inventaire du logement et sur
  l'écran du voyageur. Un module ajouté plus tard le sera aussi, sans rien
  toucher.

## 0.5.5 — 2026-08-02

**La mise à jour à distance passe enfin, pour de bon.**

- L'agent parlait à Home Assistant avec son identité de module. Home Assistant
  voyait donc « un module qui veut se remplacer » et refusait — sans un mot
  d'explication. Il utilise maintenant le compte administrateur dont il dispose
  déjà : exactement ce que fait la personne qui clique sur « Mettre à jour ».
- Les erreurs de Home Assistant ne se résument plus à un numéro. « HTTP 500 »
  seul a coûté une heure ; le motif du refus est désormais conservé.

## 0.5.4 — 2026-08-02

**Le voyageur a enfin un écran : le serveur ne démarrait jamais.**

- Le serveur qui sert la page du logement était lancé avant que le Superviseur
  dont il dépend soit créé. Il plantait aussitôt, l'erreur était avalée, et
  l'agent continuait comme si de rien n'était : le port 8099 ne répondait
  simplement pas. Pas une mauvaise page, pas une page vide — rien.
- L'erreur partait bien dans le journal de l'add-on… que personne ne lit. Elle
  est maintenant criée, et l'écran de flotte la verra.

## 0.5.3 — 2026-08-02

**Le boîtier dit la bonne adresse.**

- La 0.5.2 annonçait l'adresse de son conteneur (`172.30.x.x`) au lieu de celle
  du logement : techniquement juste, inutilisable pour le joindre — et pire que
  rien, puisqu'elle ressemblait à une réponse. Elle est maintenant demandée au
  Superviseur, seul à voir les vraies interfaces de la machine.

## 0.5.2 — 2026-08-02

**Un boîtier en panne peut enfin être réparé à distance.**

- La mise à jour du module passe désormais par Home Assistant, qui en a le
  droit — et non plus par le module lui-même, à qui c'est interdit. Jusqu'ici,
  toute mise à jour demandée à distance échouait sur une erreur muette, et il
  fallait se déplacer ou demander à l'hôte de cliquer. C'était vrai pour CHAQUE
  correction, y compris celles qui réparaient une panne.
- Le nom de l'entité de mise à jour est cherché, jamais deviné : Home Assistant
  accepte en silence un nom qui n'existe pas, sans rien faire. Trois noms
  plausibles ont été essayés le 02/08 ; les trois ont semblé réussir.
- Le boîtier dit maintenant **son adresse sur son réseau** à chaque contact.
  Quand il s'est tu ce jour-là, plus rien ne permettait de le retrouver.
- Il énumère aussi ce que Home Assistant sait mettre à jour, avec les versions
  installée et proposée — de quoi diagnostiquer sans y être.

## 0.5.1 — 2026-08-02

**Le boîtier sait enfin se mettre à jour tout seul.**

- Jusqu'ici, une mise à jour demandée à distance échouait TOUJOURS, sur une
  erreur muette (« 404 »). L'agent demandait à Home Assistant de mettre à jour
  « self » — un raccourci que Home Assistant comprend pour lire des
  informations, mais pas pour installer. Il cherchait alors un module
  littéralement appelé « self », n'en trouvait aucun, et abandonnait sans rien
  expliquer. L'agent demande maintenant son vrai nom au boîtier avant d'agir.
- Chaque échec d'entretien dit désormais sur QUOI il portait. Sans cette
  précision, le serveur ne distinguait pas « cette version a encore échoué » de
  « on en essaie une autre » : il pouvait relancer sans fin. Un boîtier réel a
  ainsi tenté 26 mises à jour en une minute le 02/08/2026.

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
