# Add-ons Brightstay pour Home Assistant

Ce dépôt contient les programmes installés dans les boîtiers des logements
équipés par [Brightstay](https://brightstay.fr).

## Ajouter ce dépôt à un boîtier

Dans Home Assistant : **Paramètres → Modules complémentaires → Boutique →
⋮ → Dépôts**, puis coller :

```
https://github.com/Brightstay/addons
```

Le dépôt apparaît alors sous le nom « Brightstay », avec ses add-ons.

## Ce qu'il y a dedans

### Brightstay Hub Agent (`bs_hub_agent`)

Le programme qui relie le logement à Brightstay. Il appelle nos serveurs à
intervalle régulier, rapporte ce que le logement voit (température, ouvertures,
détecteurs), exécute ce qu'on lui demande, et sert au mur la page de la
tablette — y compris quand internet est coupé.

Sans lui, le boîtier est un Home Assistant ordinaire : il fonctionne, mais il
n'est relié à rien.

## Ce dépôt n'est pas un projet libre

Il est public parce que Home Assistant ne sait installer un programme que
depuis un dépôt public. Le code est visible ; il n'est pas réutilisable.
Voir [LICENSE](LICENSE).

Il ne contient aucun secret : les clés d'accès de chaque boîtier sont posées à
l'atelier, dans la machine, jamais dans ce dépôt.
