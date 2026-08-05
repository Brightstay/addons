#!/usr/bin/env python3
"""
Brightstay hub-agent — le « facteur » qui tourne SUR le hub du logement.

Il ferme la boucle de mise à jour à distance : le dashboard dépose des
commandes dans Supabase (table `commandes`), l'Edge Function `hub-sync` les
distribue, et CET agent les exécute contre Home Assistant, puis accuse
réception. Les blueprints/automations étant des FICHIERS, corriger un fichier
au dépôt corrige tous les logements qui le reçoivent.

Trois principes de robustesse :
  • sans état    — tout vit dans Supabase ; l'agent ne mémorise rien de local.
  • crash-only   — une commande qui échoue n'arrête pas la boucle ; un crash
                   est relancé par le superviseur (add-on HA / systemd).
  • fail-safe    — on ne recharge JAMAIS une config que le « Check config » de
                   Home Assistant refuse : un mauvais fichier ne casse pas le hub.

Stdlib uniquement (urllib) : aucune dépendance à installer.
"""
import hashlib
import hmac
import glob
import json
import re
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# La version de l'agent, telle qu'elle remonte dans la flotte et telle que
# `min_agent_version` la compare.
#
# ⚠️ Ce n'est qu'un SECOURS. Sur un vrai hub, `main()` la remplace par la version
# de l'add-on installé, que le Superviseur connaît — c'est le numéro de
# `config.yaml`, celui que l'hôte voit dans la boutique. Sinon on aurait deux
# numéros pour la même chose, et un add-on 0.3.2 se déclarerait 0.3.0.
# Le secours sert au hub de développement, qui n'a pas de Superviseur.
#
# 0.3.0 : commande `hub.inventaire` (ce que le hub voit chez l'hôte). Un hub resté
# en 0.2.0 la refuse — c'est visible dans le sort de la commande, et l'app le dit
# à l'hôte au lieu de lui reprocher de ne pas avoir branché ses appareils.
AGENT_VERSION = "0.3.0"

# Sous-arbres du dossier de config que l'agent a le droit d'écrire. Tout le
# reste (configuration.yaml, secrets, automations de l'hôte) est intouchable.
CHEMINS_AUTORISES = ("automations_brightstay/", "blueprints/", "packages/brightstay")

# Ce qu'il faut trouver dans `configuration.yaml` pour que nos fichiers soient
# LUS. Sans ces lignes, on écrit dans un dossier que personne ne regarde.
INCLUSIONS_ATTENDUES = {
    "packages/": "packages",
    "automations_brightstay/": "automations_brightstay",
}
DOMAINES_RECHARGEABLES = {"automation", "script", "template", "input_boolean",
                          "input_number", "input_select", "scene", "group"}


# =====================================================================
# ⛔ CE QUE LE BOÎTIER REFUSE DE FAIRE, MÊME SI ON LE LUI DEMANDE
#
# ⚠️ UN ORDRE DÉPOSÉ DANS LA FILE DÉCIDE DE CE QUI S'EXÉCUTE AU DOMICILE D'UN
#    CLIENT. Borner le DOSSIER où l'agent écrit ne suffit pas, car c'est le
#    contenu du fichier qui agit une fois la configuration relue. Et laisser
#    l'appel de service viser ce qu'il veut revient à n'avoir aucune limite :
#    la porte du logement est un service comme un autre.
#
# ⚠️ CE N'EST PAS UNE REDONDANCE AVEC LES LISTES DU SERVEUR. Nos routes
#    n'acceptent déjà que six types d'ordres — et c'est très bien. Mais l'agent
#    exécute ce qu'il TROUVE DANS LA FILE, pas ce que nos routes ont accepté :
#    une clé de service qui fuite, un compte administrateur pris, une politique
#    RLS trop large, et le serveur n'est plus dans le chemin. La limite doit
#    exister dans la machine, sinon elle n'existe pas.
#
# ⚠️ ET AUCUN SERVICE QUI EN APPELLE UN AUTRE. `script.turn_on`, `scene.turn_on`
#    et `automation.trigger` sont absents EXPRÈS : un script écrit sur mesure
#    peut appeler n'importe quoi, une scène peut poser l'état « déverrouillé »
#    sur une serrure. Les autoriser reviendrait à publier la liste blanche et à
#    laisser une porte de service à côté.
# =====================================================================
SERVICES_AUTORISES = {
    # Le canal de notification (routeur SQL → `hub.service notify`).
    "notify": None,                      # None = tout le domaine
    "persistent_notification": {"create", "dismiss"},
    # Le confort du logement, ce que le pad pilote déjà.
    "light":         {"turn_on", "turn_off", "toggle"},
    "switch":        {"turn_on", "turn_off", "toggle"},
    "fan":           {"turn_on", "turn_off", "toggle", "set_percentage"},
    "cover":         {"open_cover", "close_cover", "stop_cover", "set_cover_position"},
    "climate":       {"turn_on", "turn_off", "set_temperature", "set_hvac_mode"},
    "media_player":  {"turn_off", "media_pause", "media_play", "volume_set"},
    "input_boolean": {"turn_on", "turn_off", "toggle"},
    "input_number":  {"set_value"},
    "input_select":  {"select_option"},
    "homeassistant": {"turn_on", "turn_off", "update_entity"},
}

# Les domaines qui ne doivent JAMAIS apparaître — ni appelés par `hub.service`,
# ni écrits dans un fichier de configuration. `shell_command`, `command_line` et
# `python_script` exécutent du code ; `hassio` pilote la machine ; `lock` et
# `alarm_control_panel` ouvrent le logement.
#
# ⚠️ `rest_command` N'EST PAS DANS CETTE LISTE, ET C'EST VOLONTAIRE : c'est un
#    appel HTTP, pas une exécution — et c'est le tuyau par lequel nos propres
#    automatismes remontent leurs événements. L'interdire couperait la
#    surveillance du parc pour un gain nul.
DOMAINES_INTERDITS = ("shell_command", "command_line", "python_script",
                      "hassio", "lock", "alarm_control_panel")

# ⛔ LE FILET DE TEXTE, GARDÉ MAIS DÉCLASSÉ EN SECOND RIDEAU.
#
# ⚠️ CHERCHER DES MOTS DANS UN FICHIER N'EST PAS LE LIRE. Un même contenu
#    s'écrit de plusieurs façons en YAML, toutes légales et toutes acceptées
#    par Home Assistant ; une expression qui vise une mise en page ne juge que
#    cette mise en page. La lecture du fichier, plus bas, est le vrai contrôle,
#    et ces expressions ne servent qu'en secours, si la lecture échoue.
_MOTIF = "|".join(DOMAINES_INTERDITS)
_RUBRIQUE_INTERDITE = re.compile(r"^(?:%s)\s*:" % _MOTIF, re.M | re.I)
_PLATEFORME_INTERDITE = re.compile(r"platform\s*:\s*(?:%s)\b" % _MOTIF, re.I)
_APPEL_INTERDIT = re.compile(
    r"(?:service|action)\s*:\s*[\"']?(?:%s)\." % _MOTIF, re.I)
# N'importe où dans le fichier, sans se soucier de la mise en page.
_N_IMPORTE_OU = re.compile(r"\b(?:shell_command|command_line|python_script)\b"
                           r"|\b(?:hassio|lock|alarm_control_panel)\.", re.I)

# Les clés qui portent le NOM d'un service à appeler. `action` est le mot de
# Home Assistant depuis 2024.8 ; `service` l'ancien ; `service_template` le
# très ancien. ⚠️ `action:` désigne AUSSI la liste des étapes d'une
# automatisation — d'où le contrôle « seulement si la valeur est une chaîne ».
_CLES_DE_SERVICE = ("service", "action", "service_template")

try:
    # Fourni par `py3-yaml` dans l'image de l'add-on (cf. Dockerfile).
    import yaml as _yaml
except ImportError:                                   # pragma: no cover
    _yaml = None


def _lecteur_tolerant():
    """Un lecteur YAML qui ne s'étrangle pas sur les étiquettes de HA.

    `!input`, `!secret`, `!include` ne sont pas du YAML standard : un lecteur
    normal refuse le fichier, et « refusé » vaudrait « interdit ». On avale ces
    étiquettes et on garde la structure — c'est elle qu'on inspecte.
    """
    class Tolerant(_yaml.SafeLoader):
        pass
    Tolerant.add_multi_constructor("", lambda loader, suffixe, noeud: None)
    return Tolerant


def _inspecter(noeud, profondeur=0):
    """Parcourt le YAML lu et rend la raison du refus, ou None."""
    if isinstance(noeud, dict):
        for cle, valeur in noeud.items():
            nom = str(cle).strip().lower() if cle is not None else ""
            # Une rubrique de configuration : `shell_command:` en tête.
            if profondeur == 0 and nom in DOMAINES_INTERDITS:
                return "rubrique interdite : %s" % nom
            if nom == "platform" and str(valeur).strip().lower() in DOMAINES_INTERDITS:
                return "plateforme interdite : %s" % valeur
            if nom in _CLES_DE_SERVICE and isinstance(valeur, str):
                v = valeur.strip()
                # ⛔ UN SERVICE QU'ON NE PEUT PAS LIRE EST REFUSÉ.
                #    Home Assistant autorise un nom de service composé au
                #    moment où l'automatisation part ; personne ne peut donc
                #    savoir d'avance ce qu'il visera. On ne devine pas. Nos
                #    recettes nomment toutes leur service en clair, c'est une
                #    contrainte que nous respectons déjà.
                if "{{" in v or "{%" in v:
                    return "service calculé à l'exécution (invérifiable)"
                if v.split(".")[0].strip().lower() in DOMAINES_INTERDITS:
                    return "appel de service interdit : %s" % v
            raison = _inspecter(valeur, profondeur + 1)
            if raison:
                return raison
    elif isinstance(noeud, (list, tuple)):
        for element in noeud:
            raison = _inspecter(element, profondeur + 1)
            if raison:
                return raison
    return None


# ⚠️ NOS PROPRES OUTILS D'ENTRETIEN, ET RIEN QUE LES NÔTRES.
#
# ⛔ LA PREMIÈRE LISTE BLANCHE AURAIT COUPÉ LA MISE À JOUR DE LA FLOTTE.
#    Elle refusait `script` en bloc, au motif qu'un script appelle n'importe
#    quel service. C'était juste en théorie et faux en pratique : un add-on ne
#    peut pas se remplacer lui-même (le Superviseur l'interdit), et le seul
#    chemin qui marche est un script Home Assistant que NOUS installons, appelé
#    par `script.turn_on`. Il a servi ce matin même. Déployer la liste telle
#    quelle nous privait du moyen de déployer quoi que ce soit ensuite.
#
# On autorise donc ces deux domaines, mais bornés à NOS entités : le nom de la
# cible est vérifié, pas seulement celui du service. Et ça n'ajoute aucun
# pouvoir, car le contenu de ces scripts passe par `contenu_interdit()` avant
# d'être écrit — un script à nous ne peut pas appeler ce que la liste refuse.
NOS_SCRIPTS = ("script.brightstay_", "script.bs_")
NOS_MISES_A_JOUR = ("update.brightstay_",)


def _cibles(data):
    """Les entités visées par l'ordre, quelle que soit la façon de les écrire."""
    d = data if isinstance(data, dict) else {}
    cible = d.get("entity_id")
    if cible is None and isinstance(d.get("target"), dict):
        cible = d["target"].get("entity_id")
    if isinstance(cible, (list, tuple)):
        return [str(x) for x in cible]
    return [str(cible)] if cible else []


def service_autorise(domaine, service, data=None):
    """`hub.service` a-t-il le droit d'appeler ça ?"""
    if domaine in DOMAINES_INTERDITS:
        return False

    # Nos scripts d'entretien. Home Assistant les expose des deux façons :
    # `script.turn_on` avec l'entité en cible, ou le nom du script comme
    # service. Les deux sont bornées au préfixe qui est le nôtre.
    if domaine == "script":
        if service == "turn_on":
            visees = _cibles(data)
            return bool(visees) and all(c.startswith(NOS_SCRIPTS) for c in visees)
        return service.startswith(("brightstay_", "bs_"))

    # La mise à jour d'un composant, bornée à la nôtre : `update.install` sur
    # l'entité de l'agent Brightstay, jamais sur celles de l'hôte.
    if domaine == "update":
        if service != "install":
            return False
        visees = _cibles(data)
        return bool(visees) and all(c.startswith(NOS_MISES_A_JOUR) for c in visees)

    if domaine not in SERVICES_AUTORISES:
        return False
    permis = SERVICES_AUTORISES[domaine]
    return permis is None or service in permis


def contenu_interdit(contenu):
    """Ce fichier de configuration contient-il de quoi exécuter du code ?

    Rend la raison du refus, ou `None` si le fichier est acceptable.
    """
    texte = contenu or ""

    # 1. Le filet grossier, d'abord : il ne dépend d'aucune bibliothèque.
    if (_RUBRIQUE_INTERDITE.search(texte) or _PLATEFORME_INTERDITE.search(texte)
            or _APPEL_INTERDIT.search(texte) or _N_IMPORTE_OU.search(texte)):
        return "mot interdit dans le fichier (exécution ou serrure)"

    # 2. Puis la vraie lecture. C'est elle qui voit ce que le texte cache.
    if _yaml is None:                                 # pragma: no cover
        # ⛔ ON NE DÉGRADE PAS EN SILENCE. L'image de l'add-on installe
        #    `py3-yaml` ; si la bibliothèque manque, c'est que l'image n'est
        #    pas celle qu'on croit — et on écrirait alors des fichiers qu'on
        #    n'a pas su lire.
        return "lecteur YAML absent de l'image : écriture refusée par précaution"
    try:
        documents = list(_yaml.load_all(texte, Loader=_lecteur_tolerant()))
    except Exception as e:
        # Un fichier qu'on ne sait pas lire ne s'écrit pas. Home Assistant le
        # refuserait de toute façon ; autant refuser tout de suite, avec le
        # motif, plutôt que de laisser un fichier mort sur le disque.
        return "YAML illisible : %s" % str(e).splitlines()[0][:120]
    for document in documents:
        raison = _inspecter(document)
        if raison:
            return raison
    return None


# =====================================================================
# Comparer deux versions — « 0.1.0 » vs « 0.4.0 », « 2026.7.3 » vs « 2026.8 ».
# Miroir EXACT de public.version_au_moins() côté base : le serveur écarte déjà
# les hubs trop anciens, ceci est la deuxième ceinture, côté hub.
# =====================================================================
def version_au_moins(version, minimum):
    if not minimum:
        return True          # aucune exigence
    if not version:
        return False         # exigence + version inconnue : on ne parie pas
    def decouper(v):
        v = str(v).split("+")[0]
        morceaux = []
        for m in v.split("."):
            chiffres = ""
            for c in m:
                if not c.isdigit():
                    break
                chiffres += c
            if chiffres == "":
                break
            morceaux.append(int(chiffres))
        return morceaux
    a, b = decouper(version), decouper(minimum)
    if not a or not b:
        return False         # version illisible → on n'applique pas
    for i in range(max(len(a), len(b))):
        x = a[i] if i < len(a) else 0
        y = b[i] if i < len(b) else 0
        if x > y:
            return True
        if x < y:
            return False
    return True


# =====================================================================
# Client Home Assistant — REST local, Bearer token.
# =====================================================================
class HA:
    def __init__(self, base_url, token, timeout=15):
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _req(self, method, path, body=None):
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self.token)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                txt = r.read().decode()
                return json.loads(txt) if txt else {}
        except urllib.error.HTTPError as e:
            # ⚠️ « HTTP Error 500 » TOUT SEUL NE SERT À RIEN. Le 02/08/2026, un
            #    refus de Home Assistant est remonté ainsi : trois numéros, zéro
            #    raison, et il a fallu lire le code de Home Assistant pour
            #    deviner. Le corps de la réponse contient la phrase utile — on
            #    la garde.
            detail = ""
            try:
                detail = e.read().decode()[:300].strip()
            except Exception:
                pass
            # ⛔ ET SI LE CORPS NE FAIT QUE RÉPÉTER LE CODE, ON LE TAIT.
            #    On lisait « HTTP Error 400: Bad Request — 400: Bad Request » :
            #    la même chose deux fois, ce qui donne l'illusion d'une
            #    précision et n'en apporte aucune. Home Assistant renvoie
            #    souvent son propre corps sous la forme « 400: Bad Request ».
            sans_interet = detail.replace(" ", "").lower() in (
                "", ("%d:%s" % (e.code, e.reason)).replace(" ", "").lower(),
                str(e.code), str(e.reason).replace(" ", "").lower())
            raise urllib.error.HTTPError(
                e.url, e.code,
                "%s%s" % (e.reason, "" if sans_interet else " — " + detail),
                e.headers, None)

    def check_config(self):
        """Valide la config SANS l'appliquer. {'result':'valid'|'invalid','errors':...}"""
        return self._req("POST", "/api/config/core/check_config", {})

    def reload(self, domain):
        return self._req("POST", "/api/services/%s/reload" % domain, {})

    def call_service(self, domain, service, data=None):
        """Appelle un service, et dit CE QU'ON A DEMANDÉ quand ça rate.

        ⚠️ HOME ASSISTANT NE DIRA JAMAIS POURQUOI. Son refus tient en trois
           chiffres : un service qui n'existe pas et un appel auquel il manque
           une cible rendent tous les deux « 400 », avec un corps qui répète
           « 400: Bad Request ». Vérifié sur un vrai Home Assistant le
           04/08/2026 : `script.inexistant` et `light.turn_on` sans entité
           donnent le même message, au caractère près.

           On lisait donc « HTTP Error 400 » dans le journal d'un boîtier, sans
           savoir de quel appel on parlait. La seule information disponible est
           celle que NOUS possédons : ce qu'on a demandé, et à qui.
        """
        try:
            return self._req("POST", "/api/services/%s/%s" % (domain, service), data or {})
        except urllib.error.HTTPError as e:
            if e.code != 400:
                raise
            cible = ""
            if isinstance(data, dict):
                vise = data.get("entity_id") or (data.get("target") or {}).get("entity_id")
                if vise:
                    cible = " sur %s" % (", ".join(vise) if isinstance(vise, list) else vise)
            raise urllib.error.HTTPError(
                e.url, e.code,
                "« %s.%s »%s refusé par Home Assistant : ce service n'existe pas "
                "sur ce boîtier, ou il manque une cible à l'appel"
                % (domain, service, cible),
                e.headers, None)

    def state(self, entity_id):
        try:
            return self._req("GET", "/api/states/" + entity_id)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def states(self):
        return self._req("GET", "/api/states")

    def services(self):
        """Ce que Home Assistant sait faire, sur CE boîtier.

        ⚠️ ON NE DEMANDE PAS À L'HÔTE QUELLE PILE ZIGBEE IL A. Il ne le sait
           pas, et il n'a pas à le savoir : c'est une affaire d'installateur.
           Le boîtier regarde les services qu'il expose et en déduit ce qui est
           en place. Une question de moins dans un parcours, c'est une erreur
           de moins.
        """
        return self._req("GET", "/api/services")

    def config(self):
        """La version et l'état, demandés au principal intéressé.

        Indispensable sur un hub SANS Superviseur (Home Assistant en
        conteneur) : c'est la seule source de la version du cœur, donc la
        seule chose sur quoi le garde-fou de compatibilité peut s'appuyer."""
        return self._req("GET", "/api/config") or {}

    def repond(self):
        """LE test qui empêche la surveillance de mentir.

        Jusqu'ici, « le hub va bien » voulait seulement dire « l'agent a
        appelé ». Or l'agent est un processus à part : si Home Assistant est
        FIGÉ (pas planté — figé), l'agent continue d'appeler et le tableau de
        bord affiche « en ligne » pendant que le logement est mort. On demande
        donc à HA de répondre, à chaque tour, et on le dit quand il ne répond
        pas."""
        try:
            r = self._req("GET", "/api/")
            return bool(r), None
        except Exception as e:
            return False, str(e)


# =====================================================================
# Superviseur — c'est LUI qui installe, met à jour et sauvegarde.
#
# Sans cet accès (`hassio_api: true` dans le manifeste), l'agent savait
# entretenir les RECETTES mais pas la MACHINE qui les fait tourner : il ne
# pouvait ni se mettre à jour lui-même, ni mettre à jour Home Assistant, ni
# prendre une sauvegarde. Autrement dit, le réparateur ne savait pas se
# réparer — et le moindre bug dans l'agent imposait un déplacement chez
# chaque hôte. C'est ce trou-là que cette classe ferme.
#
# Deux règles de prudence, appliquées partout ci-dessous :
#   • on ne dit JAMAIS « dernière version » — on nomme la version voulue,
#     qui vient du serveur. Une flotte ne se met pas à jour au hasard.
#   • les opérations longues (mise à jour, sauvegarde, redémarrage) sont
#     ACQUITTÉES AVANT d'être lancées : une mise à jour de l'agent tue le
#     processus, et une commande jamais acquittée serait rejouée sans fin.
# =====================================================================
class Supervisor:
    def __init__(self, token, base="http://supervisor", timeout=60):
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _req(self, method, path, body=None, timeout=None):
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self.token)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
            txt = r.read().decode()
            rep = json.loads(txt) if txt else {}
            # Le Superviseur enveloppe tout dans {result, data}
            return rep.get("data", rep)

    def texte(self, path, timeout=None):
        """Une réponse EN TEXTE, pas en JSON.

        ⛔ `_req` fait `json.loads` sur tout. Les journaux (`/core/logs`,
           `/addons/self/logs`…) sont du texte brut : le lecteur normal aurait
           levé une erreur de décodage à la première ligne, et le message
           n'aurait parlé que de JSON — jamais du journal qu'on essayait de
           lire. Deux formats, deux lecteurs.
        """
        req = urllib.request.Request(self.base + path, method="GET")
        req.add_header("Authorization", "Bearer " + self.token)
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
            return r.read().decode("utf-8", "replace")

    # --- lecture ------------------------------------------------------
    def info(self):
        """L'identité de la machine elle-même.

        On en retient `machine_id` : une empreinte que le système se donne au
        premier démarrage. Elle n'est imprimée nulle part (le numéro de série
        sous le boîtier, lui, n'est PAS lisible par le logiciel), et elle change
        si la machine est réinstallée. C'est ce qui permet de savoir qu'un hub a
        été refait — ou qu'une clé de hub a été recopiée sur une AUTRE machine."""
        return self._req("GET", "/info")

    def info_core(self):
        return self._req("GET", "/core/info")

    def info_self(self):
        return self._req("GET", "/addons/self/info")

    def info_addons(self):
        return self._req("GET", "/addons")

    def info_os(self):
        return self._req("GET", "/os/info")

    def liste_sauvegardes(self):
        return self._req("GET", "/backups")

    # --- écriture (long : voir la règle ci-dessus) ---------------------
    def recharger_boutique(self):
        """Relire la boutique avant d'installer une version.

        ⚠️ SANS ÇA, « ON DÉCIDE LA VERSION » EST UN VŒU. Le Superviseur ne relit
        notre dépôt que de temps en temps, de lui-même. Un boîtier dont l'index
        date d'hier ne CONNAÎT pas la version qu'on lui demande : il refuse,
        et le refus ressemble à une panne alors que tout va bien."""
        self._req("POST", "/store/reload", {}, timeout=300)
        return {"boutique": "relue"}

    def version_boutique(self, slug="self"):
        """La version que la boutique propose AUJOURD'HUI pour cet add-on."""
        infos = self._req("GET", "/addons/%s/info" % slug) or {}
        return infos.get("version_latest")

    def maj_addon(self, slug="self", version=None, ha=None):
        """Mettre l'add-on à jour.

        ⚠️ ON NE CHOISIT PAS LA VERSION. Éprouvé sur un vrai boîtier le 29/07 :
        le Superviseur REFUSE qu'on lui en passe une —
        « extra keys not allowed @ data['version'] ». Son seul geste est
        « prends ce que la boutique propose ». L'agent envoyait pourtant une
        version : la commande échouait donc à tous les coups, et personne ne
        l'avait vu faute de l'avoir lancée pour de vrai.

        Ce qu'on garde : le droit de REFUSER. Si la fiche du logement demande
        une version et que la boutique en propose une autre, on ne met pas à
        jour et on le dit. C'est ce qui empêche un boîtier de prendre une
        nouveauté qui ne lui était pas destinée."""
        if version:
            try:
                self.recharger_boutique()
            except Exception as e:
                print("[hub-agent] boutique non relue :", e, flush=True)
            offerte = self.version_boutique(slug)
            if offerte != version:
                raise ValueError(
                    "la boutique propose %s, la fiche demande %s — le Superviseur "
                    "n'installe que ce que la boutique offre" % (offerte, version))
        # ⛔ UN MODULE NE PEUT PAS SE REMPLACER LUI-MÊME. C'est une règle du
        #    Superviseur, pas un bogue — lue dans son code le 02/08/2026 : le
        #    rôle `manager` ouvre « /store/… », mais la règle « self » exclut
        #    explicitement « update ».
        #
        #    Éprouvé sur le vrai boîtier, deux erreurs successives :
        #      · POST /addons/self/update        → 404 (il cherche un module
        #        littéralement nommé « self ») ;
        #      · POST /addons/<vrai nom>/update  → 403 (le nom est bon, le
        #        geste est interdit).
        #
        #    Home Assistant, LUI, a le droit : c'est ce que fait le bouton
        #    « Mettre à jour » de son interface. On lui demande donc d'agir à
        #    notre place, avec ses droits. Sans ça, un agent défectueux ne peut
        #    plus jamais être réparé à distance — et c'est exactement la
        #    situation où l'on en a besoin.
        vrai = slug
        if slug == "self":
            fiche = self._req("GET", "/addons/self/info") or {}
            vrai = fiche.get("slug")
            if not vrai:
                raise ValueError(
                    "le boîtier ne dit pas son propre nom de module : "
                    "impossible de le mettre à jour sans risquer d'en viser un autre")
            if ha is not None:
                # ⛔ AVEC QUELLE IDENTITÉ ON PARLE, ET ÇA CHANGE TOUT.
                #
                #    L'agent s'adresse d'ordinaire à Home Assistant à travers le
                #    Superviseur, donc AVEC SON IDENTITÉ DE MODULE. Home
                #    Assistant voit alors « un module qui veut se remplacer » et
                #    refuse — un « HTTP 500 » sans un mot d'explication, le
                #    02/08/2026.
                #
                #    Le jeton de longue durée (celui qu'on confie à la tablette)
                #    appartient, lui, à un vrai compte administrateur. C'est
                #    exactement ce que fait la personne qui clique sur
                #    « Mettre à jour » dans l'interface.
                admin = getattr(ha, "admin", None)
                client = admin or ha
                entite = entite_de_mise_a_jour(client, fiche)
                if entite:
                    # ⛔ HOME ASSISTANT NE VOIT PAS LA NOUVELLE VERSION TOUT DE
                    #    SUITE, ET ON NE L'ATTEND PAS : ON LE LUI ORDONNE.
                    #
                    #    Le Superviseur relit la boutique quand on le lui
                    #    demande ; l'entité de mise à jour de Home Assistant,
                    #    elle, se rafraîchit à SON rythme. Entre les deux, elle
                    #    annonce « installée 0.5.17 → proposée 0.5.17 » alors
                    #    que la 0.5.18 est publiée depuis dix minutes. Lui
                    #    demander d'installer une version qu'elle ne voit pas
                    #    rend 403 ou 500 selon l'humeur — c'est ce qui a fait
                    #    échouer SEPT mises à jour les 03 et 04/08/2026, en
                    #    donnant chaque fois un code différent et aucune piste.
                    #
                    #    `homeassistant.update_entity` force la relecture. Une
                    #    seconde, et la version apparaît.
                    try:
                        client.call_service("homeassistant", "update_entity",
                                            {"entity_id": entite})
                        time.sleep(2)
                    except Exception as e:
                        print("[hub-agent] rafraîchissement de %s refusé : %s"
                              % (entite, str(e)[:90]), flush=True)
                    # ⛔ ON ESSAIE, PUIS ON EXPLIQUE — on ne refuse pas d'avance.
                    #    Le 03/08, l'agent a rendu « HTTP Error 403: Forbidden »,
                    #    trois fois, sans jamais dire quoi faire. La cause la plus
                    #    probable est connue : pas de compte administrateur. On la
                    #    dit AU MOMENT de l'échec, sans présumer qu'elle est la
                    #    seule — un refus d'avance interdirait des cas qui
                    #    marchent (un `ha` déjà administrateur, par exemple).
                    # ⛔ LE SCRIPT D'ABORD, L'APPEL DIRECT EN SECOURS — ET PAS
                    #    L'INVERSE. L'appel direct demande à Home Assistant de
                    #    remplacer l'agent PENDANT que l'agent tient la
                    #    connexion : le conteneur meurt au milieu de la requête,
                    #    plus personne ne répond, et Home Assistant rend 500.
                    #    Ce n'est pas un défaut de droits, c'est une branche
                    #    coupée pendant qu'on est assis dessus.
                    #    Le script, lui, découple : Home Assistant l'exécute
                    #    pour son compte, et l'agent peut mourir tranquillement.
                    try:
                        ha.call_service("script", "turn_on",
                                        {"entity_id": "script." + SCRIPT_SECOURS})
                        return {"addon": vrai, "par": "script." + SCRIPT_SECOURS,
                                "version": version or "celle de la boutique"}
                    except Exception as e0:
                        print("[hub-agent] script de secours indisponible (%s) — "
                              "on tente l'appel direct" % str(e0)[:80], flush=True)
                    try:
                        client.call_service("update", "install", {"entity_id": entite})
                    except Exception as e:
                        # ⛔ LA PORTE D'À CÔTÉ. Si Home Assistant refuse l'appel
                        #    direct, on déclenche le script qu'on a posé chez
                        #    lui : il fait le même geste, mais c'est LUI qui
                        #    l'exécute, avec ses droits. C'est ce qui a
                        #    débloqué le boîtier le 04/08 après six refus.
                        try:
                            ha.call_service("script", "turn_on",
                                            {"entity_id": "script." + SCRIPT_SECOURS})
                            print("[hub-agent] appel direct refusé (%s) — passé par le "
                                  "script de secours" % str(e)[:80], flush=True)
                            return {"addon": vrai, "par": "script." + SCRIPT_SECOURS,
                                    "version": version or "celle de la boutique"}
                        except Exception as e2:
                            print("[hub-agent] script de secours indisponible :",
                                  str(e2)[:120], flush=True)
                        if admin is None:
                            raise PermissionError(
                                "%s — aucun compte administrateur sur ce boîtier. "
                                "Un module n'a pas le droit de se remplacer lui-même, "
                                "et sans jeton l'agent ne peut pas le demander à Home "
                                "Assistant. Posez-le avec « node dev/jeton-tablette.mjs » "
                                "depuis le réseau du logement." % e) from e
                        raise
                    return {"addon": vrai, "par": entite,
                            "version": version or "celle de la boutique"}
                # Pas d'entité : on tente quand même la porte du Superviseur.
                # Elle refusera pour nous-même, mais le message sera clair et
                # l'échec dira POURQUOI, au lieu d'un 404 nu.
                print("[hub-agent] aucune entité de mise à jour pour %s — "
                      "on tente le Superviseur (il refusera si c'est nous-même)"
                      % vrai, flush=True)

        self._req("POST", "/store/addons/%s/update" % vrai, {}, timeout=900)
        return {"addon": vrai, "version": version or "celle de la boutique"}

    def maj_core(self, version):
        if not version:
            raise ValueError("version cible obligatoire — on ne met jamais à jour « au dernier »")
        self._req("POST", "/core/update", {"version": version}, timeout=1800)
        return {"core": version}

    def redemarrer_core(self):
        self._req("POST", "/core/restart", {}, timeout=600)
        return {"core": "redémarré"}

    def creer_sauvegarde(self, nom, mot_de_passe=None):
        corps = {"name": nom}
        if mot_de_passe:
            corps["password"] = mot_de_passe
        r = self._req("POST", "/backups/new/full", corps, timeout=1800)
        return {"sauvegarde": (r or {}).get("slug"), "nom": nom, "chiffree": bool(mot_de_passe)}

    def info_host(self):
        return self._req("GET", "/host/info")

    def info_reseau(self):
        """Les interfaces réseau de la MACHINE, pas du conteneur.

        ⚠️ CE DÉTOUR EST INDISPENSABLE. L'agent tourne dans un conteneur : lui
        demander « quelle est ton adresse » rend `172.30.33.x`, le réseau
        interne de Docker — inutilisable pour joindre le boîtier, et surtout
        trompeur : on croit avoir la réponse. Seul le Superviseur voit les
        vraies interfaces du système."""
        return self._req("GET", "/network/info")

    def redemarrer_hote(self):
        self._req("POST", "/host/reboot", {}, timeout=60)
        return {"hote": "redémarrage demandé"}

    # -----------------------------------------------------------------
    # POSER UN MODULE — pour que l'hôte n'ait pas à le faire
    #
    # ⛔ BORNÉ À CEUX DU KIT, ET C'EST LA MÊME RÈGLE QU'HIER. Installer un
    #    module quelconque à distance, c'est faire tourner du code d'un tiers
    #    sur la machine d'un client. La liste est courte, elle est ici, et elle
    #    ne se passe pas en paramètre.
    # -----------------------------------------------------------------
    MODULES_POSABLES = {
        "core_mosquitto": "Mosquitto broker",
        "45df7312_zigbee2mqtt": "Zigbee2MQTT",
    }

    def poser_addon(self, slug):
        """Installe le module s'il manque, et le démarre. Idempotent."""
        if slug not in self.MODULES_POSABLES:
            raise ValueError("module hors du périmètre Brightstay : %s" % slug)
        deja = None
        try:
            deja = self._req("GET", "/addons/%s/info" % slug)
        except Exception:
            deja = None
        pose = bool(deja and (deja.get("data") or deja).get("version"))
        if not pose:
            # La boutique d'abord : sur un boîtier qui n'a jamais rien installé,
            # le dépôt communautaire n'est pas encore lu, et l'installation
            # échoue sur un « inconnu » qui ne dit pas qu'il suffisait d'attendre.
            try:
                self.recharger_boutique()
            except Exception:
                pass
            self._req("POST", "/store/addons/%s/install" % slug, {}, timeout=1800)
        return {"slug": slug, "deja_pose": pose}

    def regler_addon(self, slug, options):
        if slug not in self.MODULES_POSABLES:
            raise ValueError("module hors du périmètre Brightstay : %s" % slug)
        self._req("POST", "/addons/%s/options" % slug, {"options": options}, timeout=120)
        return {"slug": slug, "regle": True}

    def demarrer_addon(self, slug):
        if slug not in self.MODULES_POSABLES:
            raise ValueError("module hors du périmètre Brightstay : %s" % slug)
        etat = ((self._req("GET", "/addons/%s/info" % slug) or {}).get("data") or {}).get("state")
        if etat != "started":
            self._req("POST", "/addons/%s/start" % slug, {}, timeout=600)
        return {"slug": slug, "demarre": True}

    def materiel(self):
        """Ce qui est branché sur la machine — ports série compris."""
        return self._req("GET", "/hardware/info")


# =====================================================================
# LE HUB SERT LA PAGE DU PAD.
#
# C'était le maillon manquant : tant que la tablette allait chercher son
# interface ailleurs, rien n'était « plug and play ». Quatre choses en
# découlent, toutes conditionnées à celle-ci :
#
#   • le logement ne dépend plus d'aucune machine extérieure ;
#   • la page est servie en HTTPS, donc le service worker existe, donc le pad
#     DÉMARRE MÊME BOX COUPÉE (sans ça, un hub injoignable au démarrage donne
#     une page d'erreur au voyageur) ;
#   • le pad déduit l'adresse du hub de l'endroit d'où la page a été servie —
#     plus jamais d'adresse écrite en dur ;
#   • la mise à jour de l'interface a enfin un canal.
#
# ⚠️ Pourquoi un TÉLÉCHARGEMENT et pas le canal des recettes : une recette est
# plafonnée à 64 Ko et voyage inline dans une commande. La PWA pèse 14 Mo
# (polices et images). On envoie donc une RÉFÉRENCE — une adresse et une
# empreinte — et le hub va chercher le paquet lui-même. L'empreinte est
# vérifiée avant tout déballage : un paquet qui ne correspond pas ne touche
# jamais le disque servi.
# =====================================================================
PAD_RACINE = os.environ.get("BS_PAD_RACINE", "/data/pad")
# ⚠️ PAS « PAD_PORT » : ce nom désigne déjà le port 2323 de Fully SUR la
# tablette. Deux ports différents, deux noms différents.
PAD_WEB_PORT = int(os.environ.get("BS_PAD_WEB_PORT", "8099"))
PAD_MAX_OCTETS = 64 * 1024 * 1024       # un paquet plus gros que ça est suspect
PAD_GARDE = 3                            # versions conservées (pour revenir en arrière)


# ---------------------------------------------------------------------
# L'ÉCRAN DE LA TABLETTE EST FAIT DE COUCHES QUI NE CHANGENT PAS AU MÊME
# RYTHME.
#
# C'était un bloc de 14 Mo. Corriger une ligne de la page obligeait donc
# chaque boîtier à retélécharger 14 Mo — dont 13,5 strictement identiques.
# Et le jour où un client a ses propres illustrations, ce bloc devient un
# bloc PAR CLIENT : une correction de la page se refabrique autant de fois
# qu'il y a de clients.
#
# On sert donc plusieurs dossiers, consultés dans cet ordre. Le premier qui
# a le fichier gagne :
#
#   habillage      ce qui est propre à un client, et rien d'autre
#   illustrations  les images et les polices — lourdes, rarement changées
#   page           index.html, sw.js, manifest.json — 440 Ko, souvent changée
#   complet        l'ANCIEN paquet unique
#
# ⚠️ `complet` est en dernier, et c'est ce qui rend la bascule indolore : un
# boîtier déjà en service ne connaît que lui, ne voit aucune autre couche,
# et continue de servir exactement la même chose. Il passe aux couches
# l'une après l'autre, sans jour de bascule et sans écran noir.
# ---------------------------------------------------------------------
# ⚠️ « socle » EST LE DERNIER RECOURS, ET IL EST EMBARQUÉ DANS L'ADD-ON.
#
# Un filet qu'il faut télécharger n'est pas un filet. Avant lui, un boîtier
# fraîchement installé servait 404 jusqu'à ce qu'un paquet soit publié,
# déployé ET reçu — trois choses qui peuvent manquer, et qui manquaient
# précisément le jour où l'on en avait besoin. Le socle voyage donc AVEC
# l'agent : dès que celui-ci démarre, le voyageur peut piloter la maison.
#
# Il ne se met à jour que par une version d'add-on, et c'est voulu : ce qui
# rattrape les pannes ne doit pas dépendre de la même chaîne que ce qui tombe.
COUCHES = ("habillage", "illustrations", "page", "complet", "socle")

# Là où l'add-on range son socle, à côté de ce fichier.
SOCLE_EMBARQUE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "socle")


def _pad_chemins(couche="complet"):
    """Où vivent les versions d'une couche, et quel lien désigne celle servie.

    `complet` garde l'emplacement historique : un boîtier déjà installé n'a
    rien à déménager."""
    if couche == "complet":
        return (os.path.join(PAD_RACINE, "versions"), os.path.join(PAD_RACINE, "courant"))
    # Le socle n'a ni versions ni lien : il EST le dossier, livré avec l'agent.
    # On ne le déploie pas, on ne le remplace pas, on ne le supprime pas.
    if couche == "socle":
        return (None, SOCLE_EMBARQUE)
    if couche not in COUCHES:
        raise ValueError("couche inconnue : " + str(couche))
    base = os.path.join(PAD_RACINE, "couches", couche)
    return (os.path.join(base, "versions"), os.path.join(base, "courant"))


def _chemin_dans_couches(chemin_url):
    """Le fichier demandé, cherché couche par couche dans l'ordre.

    Rend le chemin de la PREMIÈRE couche qui l'a. Si personne ne l'a, rend le
    chemin de la dernière couche : le serveur répondra 404, ce qui est la
    vérité. Rend None si la demande cherche à sortir des couches."""
    chemin = urllib.parse.unquote(chemin_url.split("?", 1)[0].split("#", 1)[0])
    morceaux = []
    for m in chemin.split("/"):
        if not m or m == ".":
            continue
        # On REFUSE une demande qui remonte, on ne la corrige pas : un chemin
        # qu'il faut réparer est un chemin qu'on n'a pas compris.
        if m == ".." or "\0" in m:
            return None
        morceaux.append(m)
    if chemin.endswith("/") or not morceaux:
        morceaux.append("index.html")

    repli = None
    for couche in COUCHES:
        _, courant = _pad_chemins(couche)
        candidat = os.path.join(courant, *morceaux)
        if os.path.exists(candidat):
            return candidat
        repli = candidat
    return repli


# ---------------------------------------------------------------------
# LA CONFIGURATION DU LOGEMENT — hors du paquet, à dessein.
#
# « Une interface différente par client » veut presque toujours dire : un
# autre logo, d'autres couleurs, un autre nom, d'autres pièces. C'est de la
# CONFIGURATION, pas du code. Si on en faisait des paquets séparés, chaque
# correction de bug serait à reconstruire et à redéployer autant de fois
# qu'il y a de clients — et 14 Mo à stocker par variante.
#
# Un seul paquet, donc, et un fichier de configuration servi À CÔTÉ. Il
# survit aux mises à jour de l'interface : il n'est pas dans le paquet.
# ---------------------------------------------------------------------
def _config_chemin():
    return os.path.join(PAD_RACINE, "config.json")


def _annonce_chemin():
    return os.path.join(PAD_RACINE, "annonce.json")


def enregistrer_annonce(corps):
    os.makedirs(PAD_RACINE, exist_ok=True)
    chemin = _annonce_chemin()
    tmp = chemin + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(corps, f, ensure_ascii=False)
    os.replace(tmp, chemin)
    # ⛔ On n'écrase PLUS l'adresse connue ici. Une annonce n'est pas une preuve :
    # elle n'est qu'une piste, que `_pad()` essaie APRÈS l'adresse déjà validée.
    # Avant, un appareil quelconque du Wi-Fi pouvait s'annoncer et se faire
    # livrer le mot de passe d'administration de la tablette.
    if corps.get("ip") and not _PAD_CONNU.get("ip"):
        _PAD_CONNU["ip"] = corps["ip"]     # rien de connu encore : mieux que rien
    return corps


def derniere_annonce():
    try:
        with open(_annonce_chemin(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def ecrire_config_pad(config, version=None):
    """Écriture atomique : le pad ne lit jamais un fichier à moitié écrit."""
    if not isinstance(config, dict):
        raise ValueError("la configuration doit être un objet")
    corps = dict(config)
    if version is not None:
        corps["_version"] = version
    chemin = _config_chemin()
    os.makedirs(PAD_RACINE, exist_ok=True)
    tmp = chemin + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(corps, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, chemin)
    return {"config": "posée", "version": corps.get("_version")}


def version_config_pad():
    """La version de configuration réellement posée sur ce hub.

    On compare des VERSIONS, pas des empreintes de JSON : deux sérialisations
    du même objet peuvent différer (ordre des clés, espaces) et feraient
    croire à un écart permanent."""
    try:
        with open(_config_chemin(), encoding="utf-8") as f:
            return json.load(f).get("_version")
    except (OSError, ValueError):
        return None


def version_pad_servie(couche="complet"):
    """La version actuellement servie — lue sur le disque, pas mémorisée.
    L'agent reste sans état : la vérité est ce qui existe."""
    _, courant = _pad_chemins(couche)
    try:
        return os.path.basename(os.path.realpath(courant)) if os.path.islink(courant) else None
    except OSError:
        return None


def couches_servies():
    """Ce que ce boîtier sert, couche par couche. C'est CE compte rendu qui
    déclenche la suite : le serveur n'envoie une couche que s'il sait déjà ce
    qui est en place (sinon on expédierait 40 Mo à l'aveugle)."""
    servies = {}
    for couche in COUCHES:
        # On ne rend PAS compte du socle : le serveur déciderait de lui envoyer
        # une version, alors qu'il n'y a rien à lui envoyer. Sa version est
        # celle de l'add-on, et elle est déjà rapportée par ailleurs.
        if couche == "socle":
            continue
        try:
            v = version_pad_servie(couche)
        except Exception:
            v = None
        if v:
            servies[couche] = v
    return servies


def version_page_servie():
    """L'empreinte de la PAGE servie — pas la version du paquet.

    Trouvé sur le Raspberry le 27/07/2026 : une interface neuve déployée sur
    le hub n'atteignait jamais la tablette, et personne ne pouvait s'en
    apercevoir. La raison tenait à deux vocabulaires : la flotte parle en
    « terrain-ece96bb2 » (empreinte du paquet, décidée au déploiement), la
    page s'annonce en « 7aa54e5e54 » (empreinte de sa construction). Deux
    nombres qui ne se comparent pas — donc « ce pad est périmé » était
    littéralement indétectable.

    Le paquet embarque désormais `version.txt` : la même empreinte que celle
    que la page annonce. Ici on la lit ; ailleurs on la compare.

    Lu À TRAVERS LES COUCHES : le fichier vient de la couche `page` sur un
    boîtier découpé, de `complet` sur un boîtier d'avant. Le même code répond
    dans les deux cas."""
    chemin = _chemin_dans_couches("/version.txt")
    if not chemin:
        return None
    try:
        with open(chemin, encoding="utf-8") as f:
            return (f.read().strip() or None)
    except OSError:
        return None


def _fichier_pad_a_rafraichir():
    return os.path.join(PAD_RACINE, "pad-a-rafraichir")


def marquer_pad_a_rafraichir(version):
    """Après un déploiement, la tablette affiche encore l'ancienne page.

    On ne s'en remet pas à une comparaison de versions au tour suivant : le
    déploiement est un ÉVÉNEMENT, et c'est lui qui doit déclencher le
    rafraîchissement. La marque est posée sur le disque parce que la tablette
    peut être injoignable à cet instant précis — on réessaiera."""
    try:
        os.makedirs(PAD_RACINE, exist_ok=True)
        with open(_fichier_pad_a_rafraichir(), "w", encoding="utf-8") as f:
            f.write(str(version or ""))
    except OSError as e:
        print("[hub-agent] marque de rafraîchissement non posée :", e, flush=True)


def rafraichir_pad_si_besoin(mot_de_passe=None):
    """Recharge la page de la tablette si une interface neuve l'attend.

    On vide le cache AVANT de recharger : sans ça, le service worker peut
    resservir la coquille précédente et le rafraîchissement ne rafraîchit
    rien. Et on ne retire la marque qu'en cas de succès — une tablette
    injoignable aujourd'hui sera rechargée demain."""
    if not os.path.exists(_fichier_pad_a_rafraichir()):
        return None
    # `_pad()` et pas `trouver_pad()` : il essaie d'abord l'adresse ANNONCÉE
    # par la tablette, puis celle qu'on connaît, et ne balaie qu'en dernier —
    # et surtout il sait retrouver le mot de passe tout seul. (Première
    # version : balayage sans mot de passe, donc jamais de tablette trouvée
    # et un rafraîchissement qui n'arrivait jamais, en silence.)
    pad = _pad(mot_de_passe)
    if pad is None:
        return None
    try:
        pad.commande("clearCache")
        pad.commande("loadStartURL")
        os.unlink(_fichier_pad_a_rafraichir())
        print("[hub-agent] tablette rechargée sur l'interface neuve", flush=True)
        return pad.ip
    except Exception as e:
        print("[hub-agent] rafraîchissement du pad remis à plus tard :", e, flush=True)
        return None


def _extraire_sur(zf, cible):
    """Déballe en refusant tout chemin qui sort du dossier — mêmes gardes que
    pour les recettes. Une archive est une entrée hostile comme une autre."""
    racine = os.path.abspath(cible)
    for membre in zf.namelist():
        if membre.startswith("/") or ".." in membre.split("/"):
            raise ValueError("chemin refusé dans l'archive : " + membre)
        dest = os.path.abspath(os.path.join(racine, membre))
        if os.path.commonpath([dest, racine]) != racine:
            raise ValueError("échappement de l'archive : " + membre)
    zf.extractall(racine)


def deployer_pad(version, url, empreinte, couche="complet"):
    """Télécharge, VÉRIFIE, déballe, puis bascule d'un coup.

    L'ordre compte : rien n'est mis en service tant que l'empreinte n'est pas
    confirmée, et la bascule est un remplacement de lien symbolique — donc
    atomique. À aucun instant le pad ne sert un dossier à moitié écrit."""
    import shutil
    import zipfile
    if not (version and url and empreinte):
        raise ValueError("version, url et empreinte sont tous obligatoires")

    # ⛔ LE SOCLE NE SE DÉPLOIE PAS. Il vient avec l'add-on, et c'est ce qui en
    # fait un filet : une commande du nuage ne doit pas pouvoir le remplacer,
    # ni le vider. Le jour où quelqu'un enverrait « hub.pad.deploy » avec
    # `couche: socle`, il emporterait le dernier recours du parc entier.
    if couche == "socle":
        raise ValueError("le socle est embarqué dans l'add-on : il ne se déploie pas")

    versions, courant = _pad_chemins(couche)
    os.makedirs(versions, exist_ok=True)
    cible = os.path.join(versions, str(version))

    if os.path.isdir(cible):
        # déjà là (re-livraison d'une commande) : on rebascule et c'est tout
        _basculer(courant, cible)
        return {"couche": couche, "version": version, "note": "déjà présente, remise en service"}

    tmp = cible + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    archive = os.path.join(versions, ".paquet.zip")
    h = hashlib.sha256()
    lu = 0
    try:
        with urllib.request.urlopen(url, timeout=120) as r, open(archive, "wb") as f:
            while True:
                bloc = r.read(65536)
                if not bloc:
                    break
                lu += len(bloc)
                if lu > PAD_MAX_OCTETS:
                    raise ValueError("paquet trop gros (> %d octets)" % PAD_MAX_OCTETS)
                h.update(bloc)
                f.write(bloc)
        obtenue = h.hexdigest()
        if obtenue.lower() != str(empreinte).lower():
            raise ValueError("empreinte fausse : attendu %s, obtenu %s" % (empreinte, obtenue))
        os.makedirs(tmp, exist_ok=True)
        with zipfile.ZipFile(archive) as zf:
            _extraire_sur(zf, tmp)
        os.replace(tmp, cible)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    finally:
        try:
            os.unlink(archive)
        except OSError:
            pass

    _basculer(courant, cible)
    _purger_versions(versions, courant)
    return {"couche": couche, "version": version, "octets": lu, "empreinte": empreinte}


def _basculer(courant, cible):
    """Remplacement ATOMIQUE du lien : le serveur ne voit jamais d'entre-deux."""
    tmp = courant + ".tmp"
    try:
        os.unlink(tmp)
    except OSError:
        pass
    os.symlink(cible, tmp)
    os.replace(tmp, courant)


def _purger_versions(versions, courant):
    """On garde les dernières : c'est ce qui rend le retour en arrière possible
    sans retélécharger quoi que ce soit."""
    import shutil
    vivante = os.path.realpath(courant)
    restes = sorted(
        (os.path.join(versions, d) for d in os.listdir(versions)
         if os.path.isdir(os.path.join(versions, d))),
        key=lambda p: os.path.getmtime(p), reverse=True)
    for p in restes[PAD_GARDE:]:
        if os.path.realpath(p) != vivante:
            shutil.rmtree(p, ignore_errors=True)


def versions_pad_disponibles(couche="complet"):
    # Le socle n'a pas d'historique : il n'y a qu'une version, celle de l'agent.
    if couche == "socle":
        return []
    versions, _ = _pad_chemins(couche)
    if not os.path.isdir(versions):
        return []
    return sorted(d for d in os.listdir(versions) if os.path.isdir(os.path.join(versions, d)))


def revenir_pad(version=None, couche="complet"):
    """Revenir à une version déjà sur le disque. Sans argument : la précédente.

    Couche par couche : on peut défaire une mauvaise page sans retélécharger
    les 40 Mo d'illustrations, qui n'y sont pour rien."""
    versions, courant = _pad_chemins(couche)
    dispo = versions_pad_disponibles(couche)
    actuelle = version_pad_servie(couche)
    if version is None:
        autres = [v for v in dispo if v != actuelle]
        if not autres:
            raise ValueError("aucune version antérieure sur le disque")
        autres.sort(key=lambda v: os.path.getmtime(os.path.join(versions, v)), reverse=True)
        version = autres[0]
    cible = os.path.join(versions, str(version))
    if not os.path.isdir(cible):
        raise ValueError("version absente du disque : " + str(version))
    _basculer(courant, cible)
    return {"couche": couche, "version": version, "note": "remise en service depuis le disque"}


# =====================================================================
# CE QUE LE HUB DIT À SA TABLETTE — né d'un constat de terrain, 27/07/2026.
#
# Sur le Raspberry, la page s'affichait parfaitement et AUCUN bouton ne
# marchait. Elle attendait qu'un humain saisisse l'adresse de Home Assistant
# et un jeton dans un panneau de réglages — et personne, dans toute la
# chaîne, ne le faisait. Les deux moitiés existaient (la fiche a une case
# `pad_config`, le hub sert bien un `/config.json`) et n'avaient jamais été
# jointes : la page ne demandait simplement jamais ce fichier.
#
# Le jeton est posé ICI, par le hub, jamais par le serveur : le nuage n'a
# aucune raison de connaître la clé de la maison, et ne doit pas pouvoir
# l'inventer.
# =====================================================================
ACCES_RESERVES = ("ha_url", "ha_token", "exclure")

# Le Superviseur, posé au démarrage : `config_pour_la_tablette` en a besoin
# pour savoir quels interrupteurs sont les nôtres, et elle est appelée depuis
# le serveur web, qui n'y a pas accès autrement.
_SUP_POUR_CONFIG = None

# =====================================================================
# L'ADRESSE DU HUB NE DOIT PAS ÊTRE ÉCRITE DANS LA FICHE.
#
# Trouvé sur le Raspberry le 27/07/2026. La fiche contenait en toutes
# lettres « http://192.168.0.30:8099/ » — l'adresse du hub, gravée au
# moment de la mise en service. On l'a fait changer d'adresse : la tablette
# a suivi, parce qu'on avait corrigé la fiche À LA MAIN. Puis on a retiré
# l'adresse sans rien dire au serveur, et là, plus rien ne rattrapait :
# le serveur continuait d'exiger une adresse qui n'existait plus, et la
# tablette était conforme… à une fiche périmée.
#
# Or une box qui redémarre redistribue les adresses. Personne ne préviendra
# le nuage.
#
# La fiche écrit donc « http://{hub}:8099/ », et c'est le hub — le seul qui
# connaisse sa propre adresse — qui remplace le marqueur au moment de poser
# le réglage. Et il fait la substitution INVERSE quand il rapporte, sinon le
# serveur croirait éternellement le réglage faux et le redemanderait sans
# fin (c'est exactement la boucle qu'on a corrigée ce matin).
# =====================================================================
MARQUE_HUB = "{hub}"


def adresse_vue_depuis(ip_cible):
    """Notre adresse TELLE QUE LA VOIT cette machine-là.

    On ne devine pas : on ouvre une socket vers elle et on demande quelle
    adresse locale le système a choisie. C'est exact même avec plusieurs
    interfaces, et ça ne dépend d'aucune configuration."""
    import socket as _s
    try:
        s = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
        try:
            s.connect((ip_cible, 9))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return None


def _substituer_hub(valeur, adresse):
    if not isinstance(valeur, str) or MARQUE_HUB not in valeur or not adresse:
        return valeur
    return valeur.replace(MARQUE_HUB, adresse)


def _remarquer_hub(valeur, adresse):
    """L'inverse : ce que le pad a vraiment, redit dans le vocabulaire de la
    fiche. Sans ça, le serveur compare « {hub} » à « 192.168.0.30 » et
    redemande le réglage à chaque tour, pour toujours."""
    if not isinstance(valeur, str) or not adresse or adresse not in valeur:
        return valeur
    return valeur.replace(adresse, MARQUE_HUB)


# =====================================================================
# LES ADRESSES QUI N'EXISTENT QUE DANS LA BOÎTE.
#
# Trouvé le 29/07/2026 sur le premier Home Assistant OS : le hub annonçait
# à sa tablette `http://172.30.33.1:8123`. C'est le réseau privé que le
# Superviseur crée entre ses conteneurs — une adresse parfaitement valable
# À L'INTÉRIEUR du boîtier, et introuvable pour tout le reste du monde.
#
# D'où elle venait : sur HA OS, notre add-on ne tourne PAS sur le réseau de
# la maison. Le port 8099 est traduit par Docker. `getsockname()` rend donc
# l'adresse du conteneur, jamais celle du hub sur la box. Sur le Raspberry,
# où l'agent tourne sans cette traduction, la même ligne rendait la bonne
# adresse — c'est pourquoi le défaut n'était jamais apparu.
#
# Ces deux plages sont fixes et documentées : 172.30.32.0/23 est le réseau
# du Superviseur, 172.17.0.0/16 le pont par défaut de Docker. Aucune box ne
# distribue ces adresses-là.
# =====================================================================
PLAGES_INTERNES = ("172.30.32.", "172.30.33.", "172.17.")


def _adresse_dans_la_maison(a):
    """Cette adresse peut-elle être atteinte depuis la tablette ?

    Non si elle est vide, si elle boucle sur elle-même, ou si elle appartient
    au réseau privé que Docker fabrique dans le boîtier."""
    if not a:
        return False
    a = a.strip().lower()
    if a in ("127.0.0.1", "::1", "0.0.0.0", "localhost"):
        return False
    if a.startswith("127.") or a.startswith("169.254."):
        return False
    return not any(a.startswith(p) for p in PLAGES_INTERNES)


def _adresse_annoncee(en_tete_host):
    """L'adresse que la TABLETTE a composée pour nous joindre.

    Filet quand `getsockname()` ne sert à rien (voir ci-dessus). On ne prend
    cet en-tête que s'il porte une ADRESSE, jamais un nom : un nom, on ne peut
    ni le vérifier ni le résoudre depuis ici, et c'est précisément ce qu'on
    glisserait pour détourner la tablette vers un faux Home Assistant.

    Le risque résiduel est nul en pratique : cet en-tête est écrit par le
    navigateur à partir de l'adresse qu'il a lui-même composée. Le seul à
    pouvoir le tordre est celui qui tient déjà la tablette."""
    if not en_tete_host:
        return None
    h = en_tete_host.strip()
    if h.startswith("["):                    # IPv6 littéral : [fd8c::1]:8099
        fin = h.find("]")
        h = h[1:fin] if fin > 0 else ""
    else:
        h = h.rsplit(":", 1)[0] if h.count(":") == 1 else h
    import ipaddress as _ip
    try:
        adr = _ip.ip_address(h)
    except ValueError:
        return None                          # un nom : on ne s'y fie pas
    if not adr.is_private or adr.is_loopback or adr.is_link_local:
        return None
    return h if _adresse_dans_la_maison(h) else None


def _adresse_joignable(url_ha, adresse_locale, en_tete_host=None):
    """L'adresse de Home Assistant TELLE QUE LA TABLETTE PEUT L'ATTEINDRE.

    Piège principal : l'agent parle à Home Assistant par `localhost`. Servir
    cette adresse-là à la tablette lui ferait chercher Home Assistant… dans
    la tablette. On remplace donc l'hôte par l'adresse du hub SUR LAQUELLE
    la tablette vient d'arriver.

    Deux sources, dans cet ordre : l'adresse de la connexion en cours, puis —
    si elle n'existe que dans le boîtier — celle que la tablette a composée.

    Si Home Assistant tourne AILLEURS que sur le hub, on n'y touche pas."""
    import urllib.parse as _parse
    u = _parse.urlsplit(url_ha or "")
    if not u.hostname:
        return None
    locaux = ("localhost", "127.0.0.1", "::1", "0.0.0.0", "supervisor")
    if u.hostname.lower() not in locaux:
        return url_ha.rstrip("/")            # HA est sur une autre machine
    adresse = adresse_locale if _adresse_dans_la_maison(adresse_locale) else None
    if adresse is None:
        adresse = _adresse_annoncee(en_tete_host)
    if adresse is None:
        return None                          # on préfère ne rien dire que mentir
    hote = "[%s]" % adresse if ":" in adresse else adresse
    port = u.port or (443 if u.scheme == "https" else 8123)
    return "%s://%s:%d" % (u.scheme or "http", hote, port)


def _jeton_pour_la_tablette(jeton):
    """Un jeton que Home Assistant acceptera — ou rien du tout.

    Trouvé le 29/07/2026 : l'add-on servait à la tablette le jeton du
    SUPERVISEUR, faute d'en avoir reçu un autre. Ce jeton-là ouvre le
    Superviseur, pas Home Assistant, qui le refuse (`auth_invalid`). La
    tablette s'affichait donc parfaitement et ne commandait rien — sans que
    rien, nulle part, ne dise pourquoi.

    Les deux se distinguent à l'œil : un jeton Home Assistant est un JWT,
    trois morceaux séparés par des points ; celui du Superviseur est une
    suite de caractères sans le moindre point. On refuse donc tout ce qui
    n'a pas la forme d'un jeton Home Assistant — mieux vaut une tablette qui
    dit « hub non connecté » qu'une tablette muette pour une raison
    invisible."""
    if not jeton or not isinstance(jeton, str):
        return None
    morceaux = jeton.strip().split(".")
    if len(morceaux) != 3 or not all(len(m) >= 8 for m in morceaux):
        return None
    return jeton.strip()


def _adresse_du_relais(adresse_locale, en_tete_host):
    """Le boîtier, vu par la tablette qui vient de l'appeler.

    ⚠️ ON REPART DE L'EN-TÊTE `Host`, c'est-à-dire de l'adresse que la tablette
    a RÉELLEMENT composée. Fabriquer l'adresse nous-mêmes, c'est se tromper le
    jour où le boîtier a deux cartes réseau, ou une adresse qui a changé — et
    la tablette se connecterait à une machine injoignable en affichant « hub
    non connecté », sans que personne comprenne pourquoi.
    """
    hote = (en_tete_host or "").strip()
    if not hote:
        if not adresse_locale:
            return None
        hote = "%s:%d" % (adresse_locale, PAD_WEB_PORT)
    # ⚠️ MÊME RÈGLE QUE LE SERVEUR, pas une seconde règle. Le serveur ne passe
    #    en HTTPS que s'il a bien ses deux fichiers de certificat sur le disque
    #    (voir `demarrer_serveur_pad`). Deviner ici indépendamment, c'est
    #    annoncer une adresse en « https » sur un serveur en clair — la
    #    tablette ne se connecterait jamais, sans dire pourquoi.
    cert, cle = os.environ.get("BS_PAD_CERT"), os.environ.get("BS_PAD_CLE")
    chiffre = bool(cert and cle and os.path.exists(cert) and os.path.exists(cle))
    schema = "https" if chiffre else "http"
    return "%s://%s" % (schema, hote)


def config_pour_la_tablette(url_ha, jeton_ha, adresse_locale, en_tete_host=None):
    """La configuration du logement + de quoi joindre Home Assistant."""
    try:
        with open(_config_chemin(), encoding="utf-8") as f:
            conf = json.load(f)
        if not isinstance(conf, dict):
            conf = {}
    except Exception:
        conf = {}                            # pas encore configuré : jamais d'erreur

    # Le nuage n'a pas voix au chapitre sur les accès : s'il en avait posé
    # (par erreur ou parce qu'on l'a compromis), il pourrait détourner la
    # tablette vers un faux Home Assistant. On les retire toujours.
    for cle in ACCES_RESERVES:
        conf.pop(cle, None)

    # Les deux ensemble, ou aucun des deux : une adresse sans jeton, ou un
    # jeton sans adresse, laisse la tablette essayer sans fin. Et un jeton que
    # Home Assistant refusera ne vaut pas mieux que pas de jeton du tout.
    # ⛔ ON N'ENVOIE PLUS LA CLÉ DE HOME ASSISTANT À LA TABLETTE.
    #
    #    Avant : `ha_url` désignait Home Assistant et `ha_token` était une clé
    #    d'ADMINISTRATEUR valable dix ans. Ce fichier étant servi sans mot de
    #    passe sur le réseau du logement, quiconque avait le Wi-Fi la lisait.
    #
    #    Maintenant : `ha_url` désigne LE BOÎTIER lui-même, et `ha_token` est un
    #    mot de passe tiré au démarrage qui n'ouvre que le relais de CE boîtier.
    #    Le lire ne permet que ce que la tablette fait déjà — allumer une lampe
    #    dans ce logement — au lieu d'ouvrir toute la domotique.
    #
    #    ⚠️ On n'annonce le relais que si l'on a VRAIMENT une clé à relayer :
    #    sinon la page se connecterait pour se faire refuser, en boucle.
    jeton = _jeton_pour_la_tablette(jeton_ha)
    moi = _adresse_du_relais(adresse_locale, en_tete_host)
    if moi and jeton:
        conf["ha_url"] = moi
        conf["ha_token"] = mot_de_passe_relais()

    # ⛔ CE QUE LA TABLETTE NE DOIT JAMAIS AFFICHER. La page lit Home Assistant
    #    directement : filtrer côté inventaire ne la protège pas. On lui donne
    #    donc la liste, calculée ici, où le Superviseur est connu.
    interdits = set(entites_de_nos_modules(_SUP_POUR_CONFIG))

    # ⛔ ET CE QUE L'EXPLOITANT INTERDIT EN PLUS, LOGEMENT PAR LOGEMENT.
    #      Le 03/08/2026, l'écran du voyageur affichait un interrupteur pour la
    #      télévision du salon. L'allumer depuis Home Assistant fait apparaître
    #      « Home Assistant » EN GRAND sur la télé — notre outil interne, sur
    #    l'écran d'un client, dans son salon. Aucun réglage ne permettait de le
    #    retirer : la liste d'exclusion ne contenait que nos propres modules,
    #    calculés ici, et n'acceptait rien de la fiche.
    #
    #    Elle accepte maintenant. C'est à l'exploitant de décider ce qu'un
    #    voyageur peut toucher — pas au hasard de ce que Home Assistant a
    #    découvert dans le logement.
    for e in (conf.get("exclure") or []):
        if isinstance(e, str) and e.strip():
            interdits.add(e.strip())

    if interdits:
        conf["exclure"] = sorted(interdits)
    return conf


# =====================================================================
# LE RELAIS VERS HOME ASSISTANT — pour que la clé ne quitte plus le boîtier.
#
# ⛔ LE DÉFAUT QU'IL SUPPRIME. Jusqu'ici, `/config.json` livrait à la tablette
#    une clé d'ADMINISTRATEUR Home Assistant valable dix ans, sur un port
#    ouvert à tout le réseau du logement. N'importe qui ayant le mot de passe
#    du Wi-Fi — un voyageur, son invité, un voisin qui l'a eu une fois — la
#    lisait en une requête, et pilotait ensuite la domotique : ouvrir ce qui
#    s'ouvre, lire l'historique de présence, éteindre les détecteurs.
#
#    Ce n'était pas un compromis pesé : personne ne l'avait décidé.
#
# CE QU'ON FAIT. La page ne parle plus à Home Assistant : elle parle au
# boîtier, sur le port qui lui sert déjà sa page. Le boîtier relaie, et
# remplace au passage le mot de passe de la page par la vraie clé — qui ne
# sort jamais de la machine.
#
# ⚠️ POURQUOI UN RELAIS ET PAS UNE AUTORISATION. On aurait pu autoriser la page
#    à parler directement à Home Assistant. Ça n'aurait rien réglé : la clé
#    serait restée dans la tablette. Le relais est ce qui la retire.
#
# ⚠️ LE MOT DE PASSE DE LA PAGE EST TIRÉ AU HASARD À CHAQUE DÉMARRAGE. Il ne
#    vaut que pour ce boîtier et ne donne accès à rien d'autre. Le lire ne
#    permet que ce que la tablette fait déjà : allumer une lampe dans CE
#    logement. C'est la différence entre une clé de chambre et le passe-partout.
# =====================================================================
CLE_WS = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"   # constante du protocole

# Tiré au démarrage. Jamais écrit sur disque : un redémarrage le renouvelle,
# et la tablette le relit dans `/config.json`.
_MDP_RELAIS = None


def _mdp_relais_chemin():
    return os.path.join(PAD_RACINE, "relais.txt")


def mot_de_passe_relais():
    """Le mot de passe que la tablette présente au relais.

    ⛔ IL SURVIT AUX REDÉMARRAGES, ET C'EST TOUT LE PROBLÈME QU'IL RÉSOUT.
       Première version : tiré à neuf à chaque démarrage de l'agent. La page du
       salon, elle, garde celui qu'elle a lu au chargement — et rien ne la fait
       le relire. Résultat observé sur le vrai boîtier dans la nuit du
       02/08/2026 : après une mise à jour de l'agent, la tablette est restée
       BLOQUÉE SUR SON ÉCRAN DE CHARGEMENT toute la nuit. Pas d'erreur, pas
       d'alerte : un panneau mural inerte, et la flotte affichait « en ligne ».

       Chaque mise à jour que nous poussons aurait donc cassé l'écran de tous
       les logements, jusqu'à ce que quelqu'un recharge la page à la main.

    ⚠️ Il est écrit à côté de la configuration du logement, dans le dossier de
       données de l'add-on — pas dans le paquet de l'interface, qui est
       remplacé à chaque déploiement.
    """
    global _MDP_RELAIS
    if _MDP_RELAIS:
        return _MDP_RELAIS
    chemin = _mdp_relais_chemin()
    try:
        with open(chemin, encoding="utf-8") as f:
            garde = f.read().strip()
        if garde.startswith("relais_") and len(garde) >= 32:
            _MDP_RELAIS = garde
            return _MDP_RELAIS
    except OSError:
        pass
    _MDP_RELAIS = "relais_" + hashlib.sha256(os.urandom(32)).hexdigest()[:32]
    try:
        os.makedirs(PAD_RACINE, exist_ok=True)
        tmp = chemin + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(_MDP_RELAIS)
        os.chmod(tmp, 0o600)
        os.replace(tmp, chemin)
    except OSError as e:
        # Pas de disque : on garde en mémoire. L'écran marchera jusqu'au
        # prochain redémarrage — mieux que rien, et on le DIT.
        print("[hub-agent] ⚠ mot de passe du relais non conservé (%s) : "
              "l'écran de la tablette se figera au prochain redémarrage" % e, flush=True)
    return _MDP_RELAIS


def _ws_accept(cle_client):
    """La réponse que le protocole exige : sans elle, aucun navigateur ne suit."""
    import base64
    brut = hashlib.sha1((cle_client + CLE_WS).encode()).digest()
    return base64.b64encode(brut).decode()


def _ws_lire_trame(lire):
    """Une trame, décodée. Rend (opcode, contenu, trame_brute) ou None si fini.

    ⚠️ ON GARDE LA TRAME BRUTE. Tout ce qui n'est pas le message
    d'authentification est retransmis TEL QUEL : ne pas ré-encoder, c'est ne
    pas pouvoir se tromper sur la fragmentation, les pings, ou le binaire.
    """
    entete = lire(2)
    if len(entete) < 2:
        return None
    brut = bytearray(entete)
    opcode = entete[0] & 0x0F
    masque = bool(entete[1] & 0x80)
    taille = entete[1] & 0x7F
    if taille == 126:
        ext = lire(2); brut += ext; taille = int.from_bytes(ext, "big")
    elif taille == 127:
        ext = lire(8); brut += ext; taille = int.from_bytes(ext, "big")
    cle = b""
    if masque:
        cle = lire(4); brut += cle
    contenu = lire(taille) if taille else b""
    brut += contenu
    if masque and contenu:
        contenu = bytes(o ^ cle[i % 4] for i, o in enumerate(contenu))
    return opcode, contenu, bytes(brut)


def _ws_trame_texte(texte, masquer):
    """Une trame texte. `masquer` : vrai quand on parle EN TANT QUE client."""
    charge = texte.encode("utf-8")
    trame = bytearray([0x81])
    n = len(charge)
    bit = 0x80 if masquer else 0x00
    if n < 126:
        trame.append(bit | n)
    elif n < 65536:
        trame.append(bit | 126); trame += n.to_bytes(2, "big")
    else:
        trame.append(bit | 127); trame += n.to_bytes(8, "big")
    if masquer:
        cle = os.urandom(4)
        trame += cle
        trame += bytes(o ^ cle[i % 4] for i, o in enumerate(charge))
    else:
        trame += charge
    return bytes(trame)


def portes_vers_home_assistant(adresse_ha):
    """Les adresses par lesquelles l'agent peut atteindre Home Assistant.

    ⚠️ ON ESSAIE, ON NE SUPPOSE PAS. Le 03/08/2026, le relais visait l'adresse
    du boîtier sur le réseau du logement : « connexion refusée ». L'agent vit
    dans un conteneur isolé — il ne joint pas la machine par son adresse
    publique. Et l'adresse qu'il utilise d'ordinaire, « http://supervisor/core »,
    est une porte de service où le chemin « /api/websocket » n'existe pas.

    Aucune de ces adresses n'est « la bonne » dans l'absolu : elle dépend de
    l'installation. On les essaie donc dans l'ordre du plus probable au moins,
    et on garde celle qui répond.
    """
    from urllib.parse import urlparse
    portes = [
        # Le nom que toute installation supervisée donne à Home Assistant.
        ("http://homeassistant:8123", "/api/websocket"),
        ("http://172.30.32.1:8123", "/api/websocket"),
    ]
    if adresse_ha:
        u = urlparse(adresse_ha)
        base = "%s://%s" % (u.scheme, u.netloc)
        chemin = (u.path or "").rstrip("/")
        # « http://supervisor/core » → la porte de service, dont le chemin
        # d'écoute permanente est « /core/websocket » et non « /core/api/… ».
        if chemin:
            portes.append((base, chemin + "/websocket"))
            portes.append((base, chemin + "/api/websocket"))
        else:
            portes.append((base, "/api/websocket"))
    vues = set()
    return [p for p in portes if not (p in vues or vues.add(p))]


def _ouvrir_amont(adresse_ha):
    """Ouvre l'écoute permanente vers Home Assistant. Rend (prise, octets_en_trop)."""
    import base64
    import socket as _sock
    from urllib.parse import urlparse

    dernier = None
    for base, chemin in portes_vers_home_assistant(adresse_ha):
        u = urlparse(base)
        hote = u.hostname
        port = u.port or (443 if u.scheme == "https" else 80)
        amont = None
        try:
            # ⚠️ COURT, ET C'EST DÉLIBÉRÉ. On essaie jusqu'à quatre portes :
            #    à cinq secondes chacune, la tablette attendrait vingt secondes
            #    devant un écran de chargement avant le premier octet. Une porte
            #    qui existe répond en quelques millisecondes sur un réseau
            #    local ; une qui n'existe pas doit être abandonnée vite.
            amont = _sock.create_connection((hote, port), timeout=1.5)
            if u.scheme == "https":
                import ssl as _ssl
                amont = _ssl.create_default_context().wrap_socket(amont, server_hostname=hote)
            cle = base64.b64encode(os.urandom(16)).decode()
            entetes = [
                "GET %s HTTP/1.1" % chemin,
                "Host: %s:%d" % (hote, port),
                "Upgrade: websocket",
                "Connection: Upgrade",
                "Sec-WebSocket-Key: %s" % cle,
                "Sec-WebSocket-Version: 13",
            ]
            # La porte de service exige le jeton du Superviseur ; Home Assistant
            # l'ignore. Le poser toujours coûte moins qu'un cas particulier.
            jeton_sup = os.environ.get("SUPERVISOR_TOKEN")
            if jeton_sup:
                entetes.append("Authorization: Bearer %s" % jeton_sup)
            amont.sendall(("\r\n".join(entetes) + "\r\n\r\n").encode())

            amont.settimeout(4)
            tampon = b""
            while b"\r\n\r\n" not in tampon:
                bloc = amont.recv(4096)
                if not bloc:
                    raise OSError("raccroché pendant la poignée de main")
                tampon += bloc
            entete, reste = tampon.split(b"\r\n\r\n", 1)
            if b"101" not in entete.split(b"\r\n")[0]:
                raise OSError(entete.split(b"\r\n")[0].decode(errors="replace"))
            amont.settimeout(None)
            print("[hub-agent] relais : Home Assistant joint par %s%s" % (base, chemin),
                  flush=True)
            return amont, reste
        except Exception as e:
            dernier = "%s%s → %s" % (base, chemin, e)
            if amont is not None:
                try:
                    amont.close()
                except Exception:
                    pass
    raise OSError("aucune porte vers Home Assistant (dernière : %s)" % dernier)


def _relayer_websocket(client, adresse_ha, jeton_reel, mdp_attendu, cle_client):
    """Relie la page à Home Assistant, en remplaçant le mot de passe par la clé.

    ⛔ CE QUI EST VÉRIFIÉ ICI, ET NULLE PART AILLEURS : le message
    d'authentification de la page DOIT porter le mot de passe du relais. Sans
    ce contrôle, n'importe qui sur le réseau du logement se ferait relayer vers
    Home Assistant avec NOS droits d'administrateur — on aurait déplacé le
    trou, pas bouché.
    """
    amont, reste = _ouvrir_amont(adresse_ha)

    client.sendall((
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Accept: %s\r\n\r\n" % _ws_accept(cle_client)
    ).encode())

    en_attente = {"amont": bytearray(reste)}

    def lire_de(sock, cle_tampon):
        def _lire(n):
            t = en_attente.setdefault(cle_tampon, bytearray())
            while len(t) < n:
                bloc = sock.recv(65536)
                if not bloc:
                    return bytes(t[:n]) if n <= len(t) else b""
                t += bloc
            out = bytes(t[:n]); del t[:n]
            return out
        return _lire

    lire_amont = lire_de(amont, "amont")
    lire_client = lire_de(client, "client")

    # 1. Home Assistant demande l'authentification : on transmet tel quel.
    t = _ws_lire_trame(lire_amont)
    if t is None:
        raise OSError("Home Assistant n'a rien dit")
    client.sendall(t[2])

    # 2. La page répond. C'est LA trame qu'on remplace.
    t = _ws_lire_trame(lire_client)
    if t is None:
        raise OSError("la page a raccroché")
    autorise = False
    try:
        msg = json.loads(t[1].decode("utf-8"))
        autorise = (msg.get("type") == "auth"
                    and hmac.compare_digest(str(msg.get("access_token") or ""), mdp_attendu))
    except Exception:
        autorise = False
    if not autorise:
        # ⛔ On refuse comme Home Assistant refuserait : la page réessaiera et
        #    dira « accès refusé », au lieu de rester figée sans explication.
        client.sendall(_ws_trame_texte(json.dumps(
            {"type": "auth_invalid", "message": "mot de passe du relais invalide"}), False))
        try:
            amont.close()
        except Exception:
            pass
        return
    amont.sendall(_ws_trame_texte(json.dumps(
        {"type": "auth", "access_token": jeton_reel}), True))

    # 3. Le reste passe en aveugle, dans les deux sens.
    import select
    socks = [client, amont]
    try:
        while True:
            prets, _, _ = select.select(socks, [], [], 300)
            if not prets:
                break
            for s in prets:
                autre = amont if s is client else client
                cle_t = "client" if s is client else "amont"
                t = en_attente.get(cle_t)
                if t:
                    autre.sendall(bytes(t)); del t[:]
                bloc = s.recv(65536)
                if not bloc:
                    return
                autre.sendall(bloc)
    finally:
        for s in socks:
            try:
                s.close()
            except Exception:
                pass


def demarrer_serveur_pad(ha_url=None, ha_token=None, ha=None, sup=None):
    """Sert le dossier courant, en HTTPS si le hub a son certificat.

    Sans certificat, on sert quand même en clair — mais on le DIT : sans
    contexte sécurisé, pas de service worker, donc pas de démarrage hors
    ligne. C'est utilisable en développement, jamais en logement."""
    import functools
    import threading
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    # `config_pour_la_tablette` est appelée depuis le serveur web, qui n'a pas
    # le Superviseur sous la main : on le lui laisse ici.
    global _SUP_POUR_CONFIG
    _SUP_POUR_CONFIG = sup

    versions, courant = _pad_chemins()
    os.makedirs(versions, exist_ok=True)

    class Poli(SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            """LA TABLETTE SE SIGNALE.

            Le hub appelle normalement la tablette (port 2323). Mais certaines
            box isolent les clients Wi-Fi les uns des autres : ce sens-là est
            alors bloqué. Le sens INVERSE, lui, fonctionne toujours — c'est
            par là que la tablette charge sa page.

            Elle nous dit donc elle-même qu'elle est vivante, et à quelle
            adresse. Trois gains : on n'a plus à deviner son adresse (donc
            plus de balayage à l'aveugle, ni d'hypothèse sur la taille du
            réseau), on la sait vivante même sous isolation, et si l'annonce
            arrive alors que le balayage échoue, on sait que c'est
            l'isolation — pas une tablette morte."""
            route = self.path.split("?")[0]
            if route == "/maintenance":
                return self._maintenance()
            if route != "/annonce":
                self.send_response(404); self.end_headers(); return
            try:
                n = int(self.headers.get("content-length") or 0)
                corps = json.loads(self.rfile.read(min(n, 8192)) or b"{}")
                if not isinstance(corps, dict):
                    corps = {}
            except Exception:
                corps = {}
            # l'adresse vient de la CONNEXION, jamais de ce que la page déclare
            corps["ip"] = self.client_address[0]
            corps["vu_a"] = _now_iso()
            try:
                enregistrer_annonce(corps)
            except Exception:
                pass
            # ⚠️ Une annonce ne PROUVE rien : n'importe quel appareil du Wi-Fi
            # du logement peut en poster une. Elle est enregistrée comme une
            # piste, et `_pad()` n'y recourt qu'après avoir échoué à joindre
            # l'adresse déjà connue — sinon le hub serait détourné vers un
            # inconnu à qui il enverrait le mot de passe de la tablette.
            self.send_response(204); self.end_headers()

        def _repondre(self, code, objet):
            corps = json.dumps(objet).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(corps)))
            self.end_headers()
            self.wfile.write(corps)

        def _maintenance(self):
            """REDÉMARRER HOME ASSISTANT DEPUIS LA TABLETTE.

            Pourquoi ici et pas ailleurs : c'est le seul endroit que la tablette
            sait déjà joindre. Sa page vient de ce serveur — même origine, rien
            à ouvrir, aucun navigateur à convaincre. Et surtout : elle n'a pas
            besoin du jeton de Home Assistant pour ça.

            ⛔ LE CODE N'EST PAS DANS LA PAGE. Elle envoie ce que l'hôte a tapé,
            le hub compare, et répond oui ou non. Un code servi à la tablette
            serait lisible par tout le Wi-Fi du logement — donc pas un code.

            ⛔ ET CE N'EST QUE `core.restart`. Redémarrer la MACHINE couperait
            l'agent : plus aucun chemin de retour, et personne dans le logement
            pour rebrancher. On ne met pas ça à portée d'un salon."""
            try:
                n = int(self.headers.get("content-length") or 0)
                corps = json.loads(self.rfile.read(min(n, 2048)) or b"{}")
                if not isinstance(corps, dict):
                    corps = {}
            except Exception:
                corps = {}

            # Sans Superviseur (essais, poste de développement), il n'y a rien
            # à redémarrer : la porte n'existe pas plutôt que d'échouer plus tard.
            attendu = _code_maintenance() if sup is not None else None
            if not attendu:
                # Fermé par défaut : sans code posé, aucune porte.
                return self._repondre(403, {"ok": False, "raison": "indisponible"})

            if _maintenance_verrouillee():
                return self._repondre(429, {"ok": False, "raison": "trop d'essais"})

            propose = str(corps.get("code") or "")
            # Comparaison à durée constante : sans elle, le temps de réponse
            # trahit le nombre de chiffres justes.
            if not hmac.compare_digest(propose, str(attendu)):
                _MAINTENANCE["essais"].append(time.time())
                return self._repondre(403, {"ok": False, "raison": "code refusé"})

            # Bon code : on repart d'une ardoise propre.
            _MAINTENANCE["essais"] = []

            depuis = time.time() - _MAINTENANCE["dernier_redemarrage"]
            if depuis < MAINTENANCE_REPOS:
                return self._repondre(429, {
                    "ok": False,
                    "raison": "déjà redémarré à l'instant",
                    "attendre_s": int(MAINTENANCE_REPOS - depuis),
                })

            # Même garde que la commande venue du serveur : on ne coupe pas
            # Home Assistant s'il ne saurait pas revenir.
            try:
                verdict = ha.check_config()
            except Exception:
                verdict = None
            if isinstance(verdict, dict) and verdict.get("result") == "invalid":
                return self._repondre(409, {"ok": False, "raison": "configuration invalide"})

            _MAINTENANCE["dernier_redemarrage"] = time.time()
            # ⚠️ La trace compte autant que le geste : un hôte qui redémarre
            # cinq fois par jour n'est pas un hôte maladroit, c'est une panne
            # qu'on ne voit pas encore.
            try:
                journal_evenement("maintenance", "info",
                                  {"operation": "redemarrer", "origine": "tablette"})
            except Exception:
                pass
            threading.Thread(target=sup.redemarrer_core, daemon=True).start()
            return self._repondre(200, {"ok": True, "attendre_s": 60})

        def do_GET(self):
            # ⛔ LE RELAIS VERS HOME ASSISTANT. C'est ici que la clé
            #    d'administrateur cesse de quitter le boîtier : la page se
            #    connecte à NOUS, pas à Home Assistant, et nous remplaçons son
            #    mot de passe par la vraie clé.
            if (self.path.split("?")[0] == "/api/websocket"
                    and "websocket" in (self.headers.get("Upgrade") or "").lower()):
                # ⚠️ L'ADRESSE DE HOME ASSISTANT VUE PAR LE BOÎTIER, PAS CELLE
                #    DU MODULE. `ha_url` vaut souvent « http://supervisor/core »
                #    — une porte interne où le chemin « /api/websocket »
                #    n'existe pas : le relais échouait, et la page ne recevait
                #    qu'une connexion coupée, sans explication.
                #    On repart de l'interface par laquelle la tablette vient de
                #    nous joindre : le boîtier sait s'atteindre lui-même.
                try:
                    mienne = self.connection.getsockname()[0]
                except Exception:
                    mienne = None
                cible = ("http://%s:8123" % mienne) if mienne else (
                    _adresse_joignable(ha_url, None, None) or ha_url)
                jeton = _jeton_pour_la_tablette(ha_token)
                if not (cible and jeton):
                    self.send_response(503); self.end_headers(); return
                try:
                    _relayer_websocket(self.connection, cible, jeton,
                                       mot_de_passe_relais(),
                                       self.headers.get("Sec-WebSocket-Key") or "")
                except Exception as e:
                    print("[hub-agent] relais interrompu :", e, flush=True)
                # La prise appartient au relais : on ne rend pas la main à
                # l'HTTP, qui écrirait par-dessus une connexion déjà détournée.
                self.close_connection = True
                return

            # /config.json ne vient PAS du paquet : il est propre au logement
            # et doit survivre à chaque mise à jour de l'interface.
            if self.path.split("?")[0] == "/config.json":
                try:
                    locale = self.connection.getsockname()[0]
                except Exception:
                    locale = None
                corps = json.dumps(
                    config_pour_la_tablette(ha_url, ha_token, locale,
                                            self.headers.get("Host")),
                    ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                # JAMAIS de cache : un jeton changé doit prendre effet au
                # rechargement suivant, pas au bon vouloir du navigateur.
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(corps)))
                self.end_headers()
                self.wfile.write(corps)
                return
            SimpleHTTPRequestHandler.do_GET(self)

        def translate_path(self, path):
            """Où aller chercher le fichier demandé.

            ⚠️ C'EST ICI QUE LA CASCADE EXISTE, et nulle part ailleurs. Le
            serveur ne connaît plus « un » dossier : il en essaie plusieurs,
            dans l'ordre, et rend le premier qui a le fichier. Un habillage
            qui ne fournit que douze images n'a donc aucun trou — les
            quarante-quatre autres viennent de la couche du dessous."""
            resolu = _chemin_dans_couches(path)
            # Une demande qui cherche à sortir des couches n'est pas réparée :
            # on rend un chemin qui n'existe pas, et le serveur répond 404.
            return resolu if resolu else os.path.join(PAD_RACINE, ".refuse")

        def end_headers(self):
            # la coquille ne doit jamais être servie depuis un cache périmé :
            # c'est elle qui porte la version du service worker.
            self.send_header("Cache-Control", "no-cache")
            SimpleHTTPRequestHandler.end_headers(self)

    # `directory=` est conservé pour les rares chemins que la bibliothèque
    # calcule elle-même, mais il ne décide plus rien : `translate_path` passe
    # devant, et c'est lui qui connaît les couches.
    srv = ThreadingHTTPServer(("0.0.0.0", PAD_WEB_PORT),
                              functools.partial(Poli, directory=courant))
    cert, cle = os.environ.get("BS_PAD_CERT"), os.environ.get("BS_PAD_CLE")
    if cert and cle and os.path.exists(cert) and os.path.exists(cle):
        import ssl as _ssl
        ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, cle)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        print("[hub-agent] page du pad servie en HTTPS sur :%d" % PAD_WEB_PORT, flush=True)
    else:
        print("[hub-agent] page du pad servie EN CLAIR sur :%d — pas de service "
              "worker, donc pas de démarrage hors ligne. Développement seulement."
              % PAD_WEB_PORT, flush=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# =====================================================================
# LE PAD — la tablette au mur.
#
# Fully Kiosk ouvre un petit serveur web sur la tablette (port 2323, réseau
# local uniquement, mot de passe). C'est par là que le hub la surveille et la
# répare : mauvaise page, écran éteint, appli passée en arrière-plan, réglage
# qui a bougé. Cent pour cent local — internet ne joue aucun rôle, et l'hôte
# n'a rien à faire.
#
# ⚠️ Limite assumée (choix « pas de routeur dans le kit ») : si le pad perd le
# Wi-Fi — box remplacée, mot de passe changé — il est HORS du réseau, donc
# hors d'atteinte de toute réparation automatique. On le signale, on ne le
# répare pas. Ne jamais promettre « zéro action humaine » sur ce cas-là.
# =====================================================================
PAD_PORT = 2323

# Les réglages qu'on sait comparer et remettre en place. Liste volontairement
# courte : un événement porte des faits, pas les 401 réglages de Fully.
REGLAGES_SUIVIS = (
    "startURL", "kioskMode", "launchOnBoot", "singleAppMode", "singleAppIntent",
    "useWideViewport", "screenBrightness", "remoteAdmin", "keepScreenOn",
    "showNavigationBar", "showActionBar", "advancedKioskProtection",
    "desktopMode", "restartOnCrash", "timeToScreensaverV2", "sleepSchedule",
    "preventSleepWhileScreenOff",
    # l'écran hors ligne (cf. dev/PROFIL-PAD.md § 4 quater)
    "errorURL", "loadContentZipFileUrl", "reloadPageFailure",
    "errorUrlOnDisconnection",
    # LE RÉSEAU. `reloadOnWifiOn` est imposé par le profil. En revanche
    # `resetWifiOnDisconnection` est seulement OBSERVÉ : il coupe le Wi-Fi dès
    # que Fully se croit déconnecté, or « déconnecté » veut dire chez lui
    # « 8.8.8.8 ne répond pas ». Dans un logement sans internet mais au hub
    # bien vivant, il couperait le Wi-Fi en boucle — précisément le cas où
    # nous promettons que tout marche. On veut savoir ce qu'il vaut sur chaque
    # tablette avant de trancher. Cf. dev/profil-pad.mjs.
    "reloadOnWifiOn", "resetWifiOnDisconnection",
)

# =====================================================================
# CE QUI SUIT EST NÉ D'UN DÉFAUT VU SUR LE TERRAIN, LE 27/07/2026.
#
# Une liste FIGÉE de réglages rapportés crée une boucle sans fin : la fiche
# demande un réglage absent de la liste, le hub le pose (il marche !), mais
# ne le rapporte jamais — donc le serveur le croit toujours manquant et le
# redemande. Au premier essai sur le Raspberry, les quatre réglages de
# l'écran hors ligne étaient renvoyés TOUTES LES 30 SECONDES, alors qu'ils
# étaient corrects sur la tablette depuis la première seconde.
#
# Rien ne cassait — c'est bien le problème : ça aurait tourné des mois.
#
# Le remède ne consiste pas à rallonger la liste (le prochain réglage
# rouvrirait le trou), mais à la rendre AUTO-EXTENSIBLE : tout réglage que
# le serveur nous a demandé de poser devient un réglage que l'on rapporte.
# La boucle se referme alors toute seule, pour n'importe quel réglage,
# y compris ceux qui n'existent pas encore.
# =====================================================================
_REGLAGES_APPRIS = set()


def _fichier_reglages_appris():
    return os.path.join(PAD_RACINE, "reglages-appris.json")


def _charger_reglages_appris():
    """Au redémarrage, on se souvient : sinon la boucle rouvre à chaque relance."""
    try:
        with open(_fichier_reglages_appris(), "r", encoding="utf-8") as f:
            _REGLAGES_APPRIS.update(json.load(f))
    except Exception:
        pass


def _apprendre_reglage(cle):
    if not cle or cle in _REGLAGES_APPRIS or cle in REGLAGES_SUIVIS:
        return
    _REGLAGES_APPRIS.add(cle)
    try:
        os.makedirs(PAD_RACINE, exist_ok=True)
        with open(_fichier_reglages_appris(), "w", encoding="utf-8") as f:
            json.dump(sorted(_REGLAGES_APPRIS), f)
    except Exception as e:
        print("[hub-agent] réglage appris non conservé (%s) : %s" % (cle, e), flush=True)


def reglages_a_rapporter():
    return set(REGLAGES_SUIVIS) | _REGLAGES_APPRIS

# Le canal vers le pad ne doit pas devenir une télécommande universelle :
# on n'accepte que les gestes de réparation.
PAD_COMMANDES_AUTORISEES = {
    "loadUrl", "screenOn", "screenOff", "toForeground",
    "clearCache", "setStringSetting", "setBooleanSetting", "deviceInfo", "listSettings",
    # ⛔ `restartApp` est VOLONTAIREMENT absent. C'est la seule commande
    # capable de couper la branche sur laquelle on est assis : le 26/07/2026,
    # un redémarrage à distance a laissé la tablette joignable au ping mais
    # sans serveur d'administration — plus aucun chemin de retour, ni pour
    # réparer, ni pour libérer. La règle était écrite dans la documentation ;
    # la porte, elle, était restée ouverte dans le code (constaté le 27/07).
    # Si un redémarrage est nécessaire, il se fait tablette EN MAIN.
}

# ─────────────────────────────────────────────────────────────────────
# OÙ EST LA TABLETTE, ET SUR QUEL RÉSEAU LA CHERCHER.
#
# ⛔ CE FUT UN CACHE EN MÉMOIRE, ET ÇA A COÛTÉ 19 HEURES D'ÉCRAN NOIR
#    (constaté le 03/08/2026). Le commentaire disait « perdu au redémarrage,
#    on re-balaie et c'est tout ». Les deux moitiés étaient fausses :
#
#      · le redémarrage n'est pas rare — il arrive à CHAQUE mise à jour de
#        l'agent, c'est-à-dire précisément au moment où l'on a le plus besoin
#        de retrouver la tablette ;
#      · « on re-balaie » ne balayait pas le bon réseau (voir `_reseaux_locaux`).
#
#    Et le troisième filet — l'annonce que poste la tablette — ne tombe que si
#    sa page s'affiche. Une tablette dont le navigateur ne peint plus rien
#    n'annonce rien. Les trois secours tombaient donc ensemble, sur la même
#    panne. On garde maintenant l'adresse SUR LE DISQUE : ce n'est pas un
#    cache, c'est ce que le boîtier sait de sa propre tablette.
_PAD_CONNU = {"ip": os.environ.get("BS_PAD_IP")}


def _fichier_pad_connu():
    return os.path.join(PAD_RACINE, "tablette.json")


def _retenir_pad(ip=None, identite=None):
    """Écrire ce qu'on vient d'apprendre de la tablette, pour le redémarrage."""
    if ip:
        _PAD_CONNU["ip"] = ip
    if identite:
        _PAD_CONNU["identite"] = identite
    try:
        os.makedirs(PAD_RACINE, exist_ok=True)
        tmp = _fichier_pad_connu() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({k: v for k, v in _PAD_CONNU.items() if v}, f)
        os.replace(tmp, _fichier_pad_connu())
    except Exception as e:
        # Ne JAMAIS faire échouer une sonde parce qu'un disque est plein : on
        # perd la mémoire, pas la fonction.
        print("[hub-agent] adresse de la tablette non conservée :", e, flush=True)


def _relire_pad_connu():
    try:
        with open(_fichier_pad_connu(), encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            for c in ("ip", "identite"):
                if d.get(c) and not _PAD_CONNU.get(c):
                    _PAD_CONNU[c] = d[c]
    except Exception:
        pass                      # première mise en route : rien à relire


_relire_pad_connu()

# LE RÉSEAU DU LOGEMENT, appris du Superviseur et conservé.
# L'add-on ne tourne PAS sur le réseau de la maison : vu de son conteneur, le
# seul réseau visible est celui de Docker (172.30.32.x). Seul le Superviseur
# voit les interfaces de la machine. On retient donc ce qu'il dit, et on le
# garde — sinon un redémarrage sans Superviseur joignable rendrait le boîtier
# aveugle une fois de plus.
_RESEAU_LOGEMENT = {"base": os.environ.get("BS_PAD_RESEAU")}


def _fichier_reseau_logement():
    return os.path.join(PAD_RACINE, "reseau.txt")


def retenir_reseau_du_logement(adresse):
    """`192.168.0.32` → on retient `192.168.0`, le réseau où chercher."""
    if not adresse or not isinstance(adresse, str):
        return None
    morceaux = adresse.strip().split(".")
    if len(morceaux) != 4 or not all(m.isdigit() for m in morceaux):
        return None
    base = ".".join(morceaux[:3])
    if base == _RESEAU_LOGEMENT.get("base"):
        return base                       # déjà connu : pas d'écriture inutile
    _RESEAU_LOGEMENT["base"] = base
    try:
        os.makedirs(PAD_RACINE, exist_ok=True)
        tmp = _fichier_reseau_logement() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(base)
        os.replace(tmp, _fichier_reseau_logement())
    except Exception as e:
        print("[hub-agent] réseau du logement non conservé :", e, flush=True)
    return base


def _relire_reseau_logement():
    if _RESEAU_LOGEMENT.get("base"):
        return
    try:
        with open(_fichier_reseau_logement(), encoding="utf-8") as f:
            base = f.read().strip()
        if base:
            _RESEAU_LOGEMENT["base"] = base
    except Exception:
        pass


_relire_reseau_logement()


# ---------------------------------------------------------------------
# L'ACCÈS À LA TABLETTE — un secret par logement, donné par le serveur.
#
# Avant : le mot de passe venait d'une variable d'environnement posée dans
# l'image dorée. Deux défauts, et le second est pire que le premier :
#   • en pratique il restait « 1234 », le même sur tous les pads — donc
#     quiconque est sur le Wi-Fi prend la main sur n'importe quelle tablette ;
#   • le changer à l'atelier faisait PERDRE LE CONTACT au hub, puisque rien
#     ne propageait la nouvelle valeur.
#
# Maintenant : la fiche du logement porte le secret, le serveur l'envoie une
# fois, et le hub le garde dans ses données persistantes. Un hub remplacé le
# reçoit à son premier contact — sans qu'on touche à son image.
# ---------------------------------------------------------------------
def _acces_chemin():
    return os.path.join(PAD_RACINE, "acces.json")


def enregistrer_acces_pad(mot_de_passe, ip=None, code_maintenance=None):
    """Les secrets du logement, rangés là où seul l'add-on les lit.

    ⚠️ `code_maintenance` NE PASSE JAMAIS PAR `/config.json`. Ce fichier-là est
    servi à qui le demande sur le Wi-Fi du logement : un code écrit dedans
    serait un code affiché sur le mur. Il arrive donc par le canal des
    secrets — le même que le mot de passe de la tablette — et c'est le HUB qui
    vérifie, jamais la page. La tablette envoie ce qu'on a tapé et reçoit
    oui ou non ; elle ne connaît pas la réponse."""
    if not mot_de_passe:
        raise ValueError("mot de passe vide")
    os.makedirs(PAD_RACINE, exist_ok=True)
    chemin = _acces_chemin()
    # Un rappel du même mot de passe ne doit pas effacer le code déjà reçu.
    ancien = {}
    try:
        with open(chemin, encoding="utf-8") as f:
            ancien = json.load(f) or {}
    except (OSError, ValueError):
        ancien = {}
    tmp = chemin + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({
            "mot_de_passe": mot_de_passe,
            "ip": ip,
            "code_maintenance": code_maintenance or ancien.get("code_maintenance"),
        }, f)
    os.replace(tmp, chemin)
    try:
        os.chmod(chemin, 0o600)      # lisible par l'add-on seul
    except OSError:
        pass
    if ip:
        _PAD_CONNU["ip"] = ip
    # on ne renvoie JAMAIS le secret dans un accusé de réception : les
    # résultats de commande sont journalisés côté serveur.
    return {"acces": "enregistré"}


def _code_maintenance():
    """Le code que l'hôte tape sur la tablette. `None` = aucun, donc tout refusé."""
    try:
        with open(_acces_chemin(), encoding="utf-8") as f:
            return (json.load(f) or {}).get("code_maintenance")
    except (OSError, ValueError):
        return None


# Ce qui empêche d'essayer les codes un par un, et de boucler les redémarrages.
_MAINTENANCE = {"essais": [], "dernier_redemarrage": 0.0}
MAINTENANCE_ESSAIS_MAX = 5          # sur un quart d'heure
MAINTENANCE_FENETRE = 900           # 15 min
MAINTENANCE_REPOS = 600             # 10 min entre deux redémarrages


def _maintenance_verrouillee(maintenant=None):
    """Trop de codes faux récemment ? On ferme la porte un moment.

    ⚠️ Six chiffres se devinent en un million d'essais — quelques heures pour
    une machine sur le même Wi-Fi. Sans ce compteur, le code ne protégerait
    rien."""
    t = maintenant if maintenant is not None else time.time()
    _MAINTENANCE["essais"] = [x for x in _MAINTENANCE["essais"] if t - x < MAINTENANCE_FENETRE]
    return len(_MAINTENANCE["essais"]) >= MAINTENANCE_ESSAIS_MAX


def _mdp_pad():
    """Le mot de passe de la tablette : l'environnement d'abord (image dorée),
    puis ce que le serveur nous a donné."""
    env = os.environ.get("BS_PAD_MOT_DE_PASSE")
    if env:
        return env
    try:
        with open(_acces_chemin(), encoding="utf-8") as f:
            d = json.load(f)
        if d.get("ip") and not _PAD_CONNU.get("ip"):
            _PAD_CONNU["ip"] = d["ip"]
        return d.get("mot_de_passe")
    except (OSError, ValueError):
        return None


class Pad:
    def __init__(self, ip, mot_de_passe, timeout=8):
        self.ip = ip
        self.mdp = mot_de_passe
        self.timeout = timeout

    def commande(self, cmd, **params):
        from urllib.parse import urlencode
        q = {"password": self.mdp, "type": "json", "cmd": cmd}
        for k, v in params.items():
            if v is not None:
                q[k] = v
        url = "http://%s:%d/?%s" % (self.ip, PAD_PORT, urlencode(q))
        with urllib.request.urlopen(url, timeout=self.timeout) as r:
            txt = r.read().decode("utf-8", "replace")
        try:
            return json.loads(txt)
        except ValueError:
            return {"reponse": txt[:200]}

    def info(self):
        return self.commande("deviceInfo")

    def reglages(self):
        return self.commande("listSettings")


def _reseaux_locaux():
    """Les réseaux où chercher le pad. On ne demandera JAMAIS à un hôte de
    figer une adresse dans le panneau de sa box : le hub trouve tout seul.

    ⚠️ Une seule piste ne suffit pas : un hub a souvent plusieurs interfaces
    (Ethernet + Wi-Fi, ponts Docker, partage de connexion). Se fier à la route
    par défaut fait chercher sur le mauvais réseau — vérifié : sur le Mac de
    test, elle désignait un partage de connexion, pas le Wi-Fi du pad."""
    import socket
    bases, vus = [], set()

    def ajouter(ip):
        if not ip or ip.startswith(("127.", "169.254.")):
            return
        base = ip.rsplit(".", 1)[0]
        if base not in vus:
            vus.add(base)
            bases.append(base)

    force = os.environ.get("BS_PAD_RESEAU")
    if force:
        return [force]                  # l'image dorée sait mieux que nous

    # ⛔ LE RÉSEAU DU LOGEMENT D'ABORD, ET IL NE VIENT PAS D'ICI.
    #
    # Tout ce qui suit lit le réseau vu DEPUIS LE CONTENEUR de l'add-on —
    # c'est-à-dire le réseau privé de Docker, `172.30.32.x`. La tablette n'y
    # est jamais. Le balayage sondait donc consciencieusement 254 adresses où
    # elle ne pouvait pas se trouver, et rendait « aucune tablette » : une
    # réponse fausse qui a l'air d'une réponse.
    #
    # C'EST EXACTEMENT L'ERREUR DÉJÀ TROUVÉE ET CORRIGÉE POUR `_adresse_locale`
    # le 02/08 (« elle a rendu 172.30.33.0, l'adresse du CONTENEUR »). La leçon
    # avait été apprise à un endroit et jamais reportée ici : le boîtier savait
    # dire où il était, et cherchait quand même au mauvais endroit. Le seul qui
    # voit les interfaces de la machine est le Superviseur — `instantane_sante`
    # nous transmet ce qu'il en dit par `retenir_reseau_du_logement`.
    if _RESEAU_LOGEMENT.get("base"):
        ajouter(_RESEAU_LOGEMENT["base"] + ".1")

    # Sur le hub (Linux), on LIT les vrais réseaux au lieu de supposer. Un
    # /24 (254 adresses) est le cas de toutes les box grand public — mais pas
    # d'un réseau d'entreprise. Deux garde-fous : on ne balaie que les réseaux
    # d'au moins 24 bits de masque, et pour les plus larges on se limite au
    # /24 qui contient le hub. Balayer 65 000 adresses ne serait pas une
    # recherche, ce serait une attaque.
    try:
        with open("/proc/net/route", encoding="utf-8") as f:
            lignes = f.read().strip().split("\n")[1:]
        for ligne in lignes:
            c = ligne.split()
            if len(c) < 8:
                continue
            dest, masque = int(c[1], 16), int(c[7], 16)
            if dest == 0 or masque == 0:
                continue          # route par défaut : pas un réseau local
            # little-endian → adresse lisible
            octets = [(dest >> (8 * i)) & 0xFF for i in range(4)]
            bits = bin(masque).count("1")
            if bits >= 24:
                ajouter("%d.%d.%d.%d" % tuple(octets))
    except OSError:
        pass                      # pas Linux (Mac de dev) : on retombe plus bas

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))     # adresse de documentation : rien n'est envoyé
        ajouter(s.getsockname()[0])
    except OSError:
        pass
    finally:
        s.close()

    try:
        for infos in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ajouter(infos[4][0])
    except OSError:
        pass

    return bases[:4]                    # au-delà, c'est du balayage pour rien


# CE QU'A DONNÉ LE DERNIER BALAYAGE. Sans ça, « aucune tablette » est un mot
# unique pour trois pannes très différentes : rien sur le réseau, mauvais
# réseau balayé, ou tablette présente qui refuse le mot de passe.
_DERNIER_BALAYAGE = {"reseaux": [], "ouverts": 0, "refus": 0, "quand": 0.0}

# ⛔ UN BALAYAGE PAR DEMI-HEURE, PAS TOUTES LES CINQ MINUTES.
#    `etat_pad` tourne à chaque tour et rebalaie dès que le pad ne répond pas
#    — c'est-à-dire EN PERMANENCE tant qu'il est en panne. Tant que le balayage
#    visait le réseau de Docker c'était inutile mais inoffensif ; depuis qu'il
#    vise le vrai réseau du logement (0.5.12), ce sont 254 adresses sondées
#    288 fois par jour CHEZ UN CLIENT. Des box le signalent comme une attaque,
#    et une petite machine y passe du temps pour rien : une tablette qui n'a
#    pas bougé en cinq minutes n'aura pas bougé non plus à l'essai suivant.
#    Entre deux balayages, l'adresse connue et l'annonce restent essayées à
#    chaque tour — c'est le chemin normal, et il est gratuit.
BALAYAGE_MIN_S = int(os.environ.get("BS_PAD_BALAYAGE_MIN", "1800"))


def trouver_pads(mot_de_passe, timeout=0.35, limite=None):
    """TOUS les pads du réseau, dans l'ordre où on les rencontre.

    Le hub n'en veut qu'un (un logement n'a qu'une tablette) et s'arrête au
    premier — c'est `trouver_pad` juste en dessous.

    ⚠️ L'ATELIER, LUI, A BESOIN DE LES VOIR TOUS. Deux tablettes neuves
    branchées en même temps répondent toutes les deux : « la première qui
    répond » en configurerait une au hasard, sans rien dire, et l'autre
    partirait chez un hôte à moitié réglée.

    Sans routeur à nous, l'adresse d'une tablette peut changer au redémarrage
    de la box. Plutôt que d'exiger une réservation d'adresse (personne ne le
    fera), on balaie le réseau et on cherche qui répond à Fully."""
    import socket
    from concurrent.futures import ThreadPoolExecutor

    def ouvert(ip):
        s = socket.socket()
        s.settimeout(timeout)
        try:
            return ip if s.connect_ex((ip, PAD_PORT)) == 0 else None
        except OSError:
            return None
        finally:
            s.close()

    trouves = []
    _DERNIER_BALAYAGE.update({"reseaux": [], "ouverts": 0, "refus": 0})
    for base in _reseaux_locaux():
        _DERNIER_BALAYAGE["reseaux"].append(base + ".0/24")
        adresses = ["%s.%d" % (base, n) for n in range(1, 255)]
        with ThreadPoolExecutor(max_workers=48) as ex:
            for ip in ex.map(ouvert, adresses):
                if not ip:
                    continue
                # ⚠️ QUELQU'UN RÉPOND ICI. On le compte AVANT d'essayer de lui
                #    parler : c'est ce chiffre qui distingue « il n'y a personne
                #    sur ce réseau » de « la tablette est là mais refuse ».
                _DERNIER_BALAYAGE["ouverts"] += 1
                try:
                    # port ouvert ≠ notre pad : on vérifie que c'est bien Fully
                    if Pad(ip, mot_de_passe, timeout=4).info().get("packageName"):
                        trouves.append(ip)
                        if limite and len(trouves) >= limite:
                            return trouves
                    else:
                        _DERNIER_BALAYAGE["refus"] += 1
                except Exception:
                    # ⛔ CE `pass` A COÛTÉ UNE NUIT. Une tablette bien vivante
                    #    qui refuse notre mot de passe tombait ici, en silence,
                    #    et le boîtier rapportait « aucune tablette » — le même
                    #    mot que pour une tablette éteinte. Deux pannes qui
                    #    n'ont RIEN à voir, un seul message. On les sépare.
                    _DERNIER_BALAYAGE["refus"] += 1
    return trouves


def trouver_pad(mot_de_passe, timeout=0.35):
    """Le premier pad du réseau — ce qu'il faut au hub, et rien de plus."""
    trouves = trouver_pads(mot_de_passe, timeout, limite=1)
    return trouves[0] if trouves else None


def _identite_pad(infos):
    """De quoi reconnaître NOTRE tablette d'une autre machine du réseau.

    Fully rend son adresse matérielle et son numéro de série ; l'un des deux
    suffit et ne change pas au gré des adresses IP."""
    if not isinstance(infos, dict):
        return None
    for cle in ("deviceID", "deviceId", "serial", "Mac", "mac"):
        v = infos.get(cle)
        if v:
            return str(v)
    return None


def _pad(mot_de_passe=None, rebalayer=True):
    """Le pad, à l'adresse qu'on lui connaît, sinon celle qu'il a ANNONCÉE,
    sinon retrouvé par balayage.

    ⚠️ CET ORDRE A CHANGÉ LE 29/07, ET C'EST UNE CORRECTION DE SÉCURITÉ.
    L'adresse annoncée passait EN PREMIER. Or n'importe quel appareil du Wi-Fi
    du logement peut poster une annonce en se donnant l'adresse qu'il veut : le
    hub la croyait, l'appelait — et lui envoyait le MOT DE PASSE
    d'administration de la tablette, que Fully exige à chaque appel.

    Deux verrous depuis :
      • l'adresse déjà connue est essayée d'abord ; une annonce ne sert plus
        que si la vraie tablette ne répond plus (le cas pour lequel l'annonce
        existe : une box qui isole les appareils entre eux) ;
      • on retient l'identité de la tablette au premier contact réussi. Une
        machine qui répond avec une autre identité est écartée, et on le note.
    """
    mdp = mot_de_passe or _mdp_pad()
    if not mdp:
        return None
    candidates = []
    if _PAD_CONNU.get("ip"):
        candidates.append(_PAD_CONNU["ip"])
    a = derniere_annonce()
    if a and a.get("ip") and a["ip"] not in candidates:
        candidates.append(a["ip"])
    for ip in candidates:
        try:
            infos = Pad(ip, mdp).info()
            if not infos.get("packageName"):
                continue
            identite = _identite_pad(infos)
            attendue = _PAD_CONNU.get("identite")
            if attendue and identite and identite != attendue:
                # Quelqu'un d'autre répond à cette adresse. On ne lui parle plus.
                print("[hub-agent] pad inattendu en %s (identité %s ≠ %s) — ignoré"
                      % (ip, identite, attendue), flush=True)
                continue
            # On ÉCRIT ce qu'on vient d'apprendre : le prochain démarrage
            # de l'agent doit repartir d'ici, pas de zéro.
            _retenir_pad(ip=ip, identite=identite if not attendue else None)
            return Pad(ip, mdp)
        except Exception:
            pass
    if not rebalayer:
        return None
    depuis = time.time() - float(_DERNIER_BALAYAGE.get("quand") or 0)
    if depuis < BALAYAGE_MIN_S:
        return None
    _DERNIER_BALAYAGE["quand"] = time.time()
    ip = trouver_pad(mdp)
    if ip:
        _retenir_pad(ip=ip)
    return Pad(ip, mdp) if ip else None


def etat_pad(mot_de_passe=None):
    """Ce que le hub voit du pad — pour que le serveur puisse le comparer à
    ce qu'il devrait être, et renvoyer le geste qui manque."""
    mdp = mot_de_passe or _mdp_pad()
    if not mdp:
        return None                      # pas de pad déclaré sur ce hub
    # L'annonce est le signal le plus important quand le reste échoue : elle
    # dit « la tablette est vivante » alors même qu'on ne peut pas la piloter.
    a = derniere_annonce() or {}
    socle = {}
    if a.get("vu_a"):
        socle = {"annonce_vu_a": a["vu_a"], "annonce_ip": a.get("ip"),
                 "annonce_version": a.get("version"), "annonce_page": a.get("page")}

    p = _pad(mdp)
    if p is None:
        # Vivante mais impilotable = la box isole les clients Wi-Fi les uns
        # des autres. Ce n'est PAS la même panne qu'une tablette éteinte, et
        # ça ne se répare pas de la même façon : on le distingue.
        socle["joignable"] = False
        if socle.get("annonce_vu_a"):
            socle["isolee"] = True
        # POURQUOI on ne l'a pas trouvée. Trois pannes portaient le même mot.
        if _DERNIER_BALAYAGE.get("reseaux"):
            socle["balayage"] = {
                "reseaux": list(_DERNIER_BALAYAGE["reseaux"]),
                "machines_ouvertes": _DERNIER_BALAYAGE["ouverts"],
                "refus": _DERNIER_BALAYAGE["refus"],
            }
            if _DERNIER_BALAYAGE["refus"]:
                # Quelqu'un répond sur le port de la tablette et n'accepte pas
                # notre mot de passe : la tablette est LÀ, c'est le secret qui
                # ne correspond pas. Panne complètement différente.
                socle["mot_de_passe_refuse"] = True
        return socle
    try:
        info = p.info()
    except Exception as e:
        return {"joignable": False, "erreur": str(e)[:120]}

    # NOTRE ADRESSE, UNE SEULE FOIS, ET SANS POUVOIR ÉCHOUER EN SILENCE.
    # Tout ce qui contient une adresse doit être RETRADUIT en `{hub}` avant de
    # partir : la fiche est écrite avec le marqueur, donc un rapport qui garde
    # l'adresse brute ne sera JAMAIS égal à ce qui est demandé — et le serveur
    # renverra le même ordre éternellement. C'est exactement ce qui s'est passé
    # le 27/07 au soir : la page (et elle seule) échappait à la traduction, et
    # la tablette recevait « recharge la page » toutes les 32 secondes alors
    # qu'elle affichait déjà la bonne.
    try:
        mienne = adresse_vue_depuis(p.ip)
    except Exception as e:
        mienne = None
        print("[hub-agent] adresse du hub introuvable — les rapports vont "
              "garder des adresses brutes et le serveur bouclera :", e, flush=True)

    etat = dict(socle)
    if etat.get("annonce_page"):
        etat["annonce_page"] = _remarquer_hub(etat["annonce_page"], mienne)
    etat.update({
        "joignable": True,
        "isolee": False,
        "ip": p.ip,
        "batterie": info.get("batteryLevel"),
        # ⚠️ TOUJOURS BRANCHÉE, DONC LA VALEUR ABSOLUE NE DIT RIEN. L'étude
        #    énergie l'a établi : « tous les relevés branchés sont à 38–40 °C
        #    quoi qu'affiche l'écran, le chargeur tient la température à lui
        #    seul ». Un pad mural est branché en permanence : c'est la DÉRIVE
        #    qui parle — par rapport à sa propre moyenne, ou aux autres du parc.
        "temperature": info.get("batteryTemperature"),
        "branche": bool(info.get("isPlugged")),
        "ecran_allume": bool(info.get("screenOn")),
        "page": _remarquer_hub(info.get("currentPage"), mienne),
        "premier_plan": info.get("foregroundApp") == info.get("packageName"),
        # L'ÉCRAN DE VERROUILLAGE ANDROID. À suivre de près : aucune
        # application ne peut franchir un verrouillage SÉCURISÉ (code, schéma,
        # empreinte) — c'est une garantie du système, pas une limite de Fully.
        # Si un pad reste verrouillé, aucun remède à distance n'existe : la
        # seule issue est d'avoir supprimé le verrou à l'atelier. On le voit
        # donc, on le signale, et on ne prétend pas le réparer.
        "verrouille": bool(info.get("keyguardLocked")),
        "ecran_verrouille": bool(info.get("screenLocked")),
        "veille_forcee": bool(info.get("isInForcedSleep")),
        "economiseur": bool(info.get("isInScreensaver")),
        "version_fully": info.get("appVersionName"),
        # ── QUI EST CETTE TABLETTE ────────────────────────────────────
        # ⚠️ CE N'EST PAS LE NUMÉRO DE LA BOÎTE, et il ne faut pas le faire
        # croire. Relevé sur banc le 26/07 : Fully rend « 05157df5d0543c12 »,
        # seize caractères hexadécimaux — un identifiant logiciel. Le numéro de
        # série imprimé, lui, ressemble à « R58M40… », et depuis Android 10 le
        # système le CACHE aux applications qui ne sont pas propriétaires de
        # l'appareil.
        # Les deux servent, mais à des questions différentes : l'étiquette pour
        # retrouver la tablette dans le monde réel, celle-ci pour savoir si
        # c'est toujours la même machine qu'à l'atelier.
        "identite": (info.get("deviceID") or info.get("deviceId")
                     or info.get("serial") or info.get("Mac") or None),
        "modele": info.get("deviceModel"),
        "android": info.get("androidVersion"),
        "proprietaire_appareil": bool(info.get("isDeviceOwner")),
    })
    try:
        tous = p.reglages() or {}
        etat["reglages"] = {k: _remarquer_hub(tous[k], mienne)
                            for k in reglages_a_rapporter() if k in tous}
        etat["hub_ip"] = mienne      # le serveur peut enfin savoir où joindre le hub
    except Exception as e:
        # NE PAS SE CONTENTER DE L'ENREGISTRER. Sans réglages rapportés, le
        # serveur les croit tous manquants et les redemande à chaque tour —
        # la boucle sans fin corrigée le matin même. Arrivé une seconde fois
        # dans la journée, à cause d'un nom de variable. Ça se dit à voix haute.
        etat["reglages_erreur"] = str(e)[:120]
        print("[hub-agent] réglages du pad NON rapportés (le serveur va "
              "les redemander sans fin) :", e, flush=True)
    return etat


def configuration_lit(config_dir, dossier):
    """Home Assistant lit-il ce dossier ?

    ⛔ LE DÉFAUT QUE ÇA CORRIGE, ET IL EST DE LA PIRE ESPÈCE. Le 03/08/2026,
       `hub.apply` a écrit un paquet dans `packages/`, a répondu
       « appliqué : packages/brightstay_maj.yaml », rechargé le domaine — et
       RIEN n'existait. `configuration.yaml` de ce boîtier ne contient aucune
       inclusion : l'agent écrivait dans un dossier que personne ne regarde,
       et affirmait un succès. Un échec franc coûte cinq minutes ; un faux
       succès a coûté une après-midi.
    """
    marqueur = INCLUSIONS_ATTENDUES.get(dossier)
    if not marqueur:
        return True                       # dossier sans exigence connue
    try:
        with open(os.path.join(config_dir, "configuration.yaml"), encoding="utf-8") as f:
            texte = f.read()
    except OSError:
        return None                       # illisible : on ne sait pas, on ne ment pas
    for ligne in texte.split("\n"):
        nue = ligne.strip()
        if nue.startswith("#"):
            continue
        if marqueur in nue and "!include" in nue:
            return True
    return False


SCRIPT_SECOURS = "brightstay_maj_agent"
FICHIER_SECOURS = "packages/brightstay_secours.yaml"


def assurer_script_secours(config_dir, entite=None):
    """Poser dans Home Assistant un script capable de mettre l'agent à jour.

    ⛔ POURQUOI L'AGENT NE PEUT PAS ÊTRE SA PROPRE ISSUE DE SECOURS.
       Un module Home Assistant n'a pas le droit de se remplacer lui-même :
       vérifié sur matériel les 03 et 04/08/2026, par TROIS chemins, tous
       refusés en 403. Il doit le demander à Home Assistant. Et si le moyen de
       le demander est cassé — c'était le cas, le jeton partait à la mauvaise
       adresse — alors le correctif est dans une version qu'on ne peut plus
       installer. Le piège s'est refermé pendant deux jours.

       Ce script est la porte d'à côté. Home Assistant l'exécute avec SES
       droits, sans rien demander à l'agent. Il suffit de le déclencher par un
       appel de service ordinaire — ce que l'agent sait faire même très abîmé.

    ⚠️ IL DORT. Il ne se déclenche sur rien : ni horaire, ni événement. Un
       script qui met à jour tout seul, c'est la tempête de 26 mises à jour en
       une minute du 02/08. Il attend qu'on l'appelle, et c'est tout.
    """
    if not entite:
        return None
    chemin = os.path.join(config_dir, FICHIER_SECOURS)
    contenu = (
        "# Posé par l'agent Brightstay — NE PAS RETIRER.\n"
        "# C'est la seule porte qui reste quand l'agent est trop abîmé pour se\n"
        "# réparer lui-même. Il dort tant que personne ne l'appelle.\n"
        "script:\n"
        "  %s:\n"
        "    alias: \"Brightstay - mise a jour de l'agent\"\n"
        "    mode: single\n"
        "    sequence:\n"
        "      - service: update.install\n"
        "        target:\n"
        "          entity_id: %s\n" % (SCRIPT_SECOURS, entite)
    )
    try:
        if os.path.exists(chemin):
            with open(chemin, encoding="utf-8") as f:
                if f.read() == contenu:
                    return True          # déjà en place, à l'identique
        os.makedirs(os.path.dirname(chemin), exist_ok=True)
        with open(chemin, "w", encoding="utf-8") as f:
            f.write(contenu)
        print("[hub-agent] script de secours posé (%s)" % entite, flush=True)
        return True
    except OSError as e:
        print("[hub-agent] script de secours NON posé :", e, flush=True)
        return False


def assurer_inclusions(config_dir, ha=None):
    """Garantir que Home Assistant LIT les dossiers où l'agent écrit.

    ⛔ LE DÉFAUT, ET IL EST DE LA PIRE ESPÈCE. Le 03/08/2026, `hub.apply` a
       écrit un paquet, répondu « appliqué », rechargé le domaine — et rien
       n'existait. `configuration.yaml` n'incluait ni `packages/` ni
       `automations_brightstay/`. L'agent écrivait dans un dossier que
       personne ne regarde, en affirmant un succès. Un échec franc coûte cinq
       minutes ; un faux succès a coûté une après-midi.

    ⚠️ ON N'AJOUTE QUE CE QUI MANQUE, ET ON VÉRIFIE APRÈS. Si la configuration
       devient invalide, on remet l'ancienne : mieux vaut un dossier non lu
       qu'un Home Assistant qui ne démarre plus.

    ⚠️ ET C'EST L'AGENT QUI LE FAIT, PAS L'ATELIER. Les boîtiers déjà partis
       n'ont jamais vu l'atelier corrigé ; ils verront cet agent-ci.
    """
    chemin = os.path.join(config_dir, "configuration.yaml")
    try:
        with open(chemin, encoding="utf-8") as f:
            avant = f.read()
    except OSError as e:
        print("[hub-agent] configuration.yaml illisible :", e, flush=True)
        return None

    ajouts = []
    utiles = [l.strip() for l in avant.split("\n") if not l.strip().startswith("#")]
    if not any("packages:" in l and "!include" in l for l in utiles):
        ajouts.append("homeassistant:\n  packages: !include_dir_named packages")
    if not any("automations_brightstay" in l for l in utiles):
        ajouts.append(
            '"automation brightstay": !include_dir_merge_list automations_brightstay/')
    if not ajouts:
        return True

    for d in ("packages", "automations_brightstay"):
        try:
            os.makedirs(os.path.join(config_dir, d), exist_ok=True)
        except OSError:
            pass

    apres = avant.rstrip("\n") + "\n\n# ── Brightstay — posé par l'agent, ne pas retirer ──\n" \
        + "\n".join(ajouts) + "\n"
    try:
        with open(chemin, "w", encoding="utf-8") as f:
            f.write(apres)
    except OSError as e:
        print("[hub-agent] inclusions non posées :", e, flush=True)
        return False

    # Vérifier, et défaire si on a cassé quelque chose.
    if ha is not None:
        try:
            v = ha.check_config() or {}
            if v.get("result") == "invalid" or v.get("errors"):
                with open(chemin, "w", encoding="utf-8") as f:
                    f.write(avant)
                print("[hub-agent] inclusions ANNULÉES (configuration invalide) :",
                      str(v.get("errors"))[:200], flush=True)
                return False
        except Exception as e:
            print("[hub-agent] configuration non vérifiée après ajout :", e, flush=True)

    print("[hub-agent] inclusions posées dans configuration.yaml :",
          len(ajouts), "— redémarrez Home Assistant pour qu'elles prennent", flush=True)
    return True


# =====================================================================
# Dépôt de fichiers — écriture GARDÉE dans le dossier de config du hub.
# =====================================================================
class Store:
    def __init__(self, config_dir):
        self.root = os.path.abspath(config_dir)

    def _resoudre(self, rel):
        # refuse l'absolu, la traversée, et tout ce qui sort des sous-arbres autorisés
        if not rel or rel.startswith("/") or ".." in rel.split("/"):
            raise ValueError("chemin refusé : " + rel)
        if not any(rel == p or rel.startswith(p) for p in CHEMINS_AUTORISES):
            raise ValueError("hors du périmètre Brightstay : " + rel)
        cible = os.path.abspath(os.path.join(self.root, rel))
        # ceinture + bretelles : la cible résolue DOIT rester sous la racine
        if os.path.commonpath([cible, self.root]) != self.root:
            raise ValueError("échappement du dossier de config : " + rel)
        return cible

    def read(self, rel):
        cible = self._resoudre(rel)
        if not os.path.exists(cible):
            return None
        with open(cible, encoding="utf-8") as f:
            return f.read()

    def put(self, rel, content):
        cible = self._resoudre(rel)
        # ⛔ LE DOSSIER NE SUFFIT PAS : C'EST LE CONTENU QUI EXÉCUTE.
        #    Écrire dans `packages/brightstay…` était réputé sans risque parce
        #    que le chemin est borné. Mais le contenu d'un fichier de
        #    configuration agit dès que Home Assistant le relit. Le périmètre
        #    protégeait les fichiers de l'hôte, pas l'hôte.
        raison = contenu_interdit(content)
        if raison:
            raise ValueError("contenu refusé (%s) : %s" % (raison, rel))
        os.makedirs(os.path.dirname(cible), exist_ok=True)
        # écriture atomique : temp + rename (jamais de fichier à moitié écrit)
        tmp = cible + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, cible)
        return {"written": rel, "bytes": len(content.encode())}

    def delete(self, rel):
        cible = self._resoudre(rel)
        if os.path.exists(cible):
            os.remove(cible)
        return {"deleted": rel}


def _rollback(store, snapshot):
    """Remet chaque fichier dans l'état d'avant : absent → supprimé, sinon restauré."""
    for rel, avant in snapshot.items():
        if avant is None:
            store.delete(rel)
        else:
            store.put(rel, avant)


# =====================================================================
# Répartition d'UNE commande → (status, result). Fonction quasi-pure :
# ses seules dépendances (ha, store) sont injectées → testable sans réseau.
# =====================================================================
def _differer(differes, nom, action, resume, cible=None):
    """Acquitte MAINTENANT, agit APRÈS que l'accusé de réception soit parti.

    Indispensable pour tout ce qui coupe le tapis sous nos pieds : mettre à
    jour l'agent tue son propre processus, mettre à jour le cœur redémarre
    Home Assistant. Si on agissait d'abord, la commande resterait sans
    réponse et le serveur la re-livrerait en boucle.
    `differes = None` ⇒ mode synchrone, pour les tests unitaires."""
    if differes is None:
        return "acked", action()
    differes.append((nom, action, cible))
    return "acked", {"lance": resume}


# =====================================================================
# OUVRIR L'APPAIRAGE ZIGBEE — soixante secondes, puis ça se referme seul
#
# ⛔ LE TROU QUE ÇA COMBLE. Notre parcours d'installation savait créer des
#    pièces et y ranger des appareils : il ne lisait QUE ce que Home Assistant
#    voyait déjà. Un hôte qui achetait un capteur n'avait aucun moyen de le
#    faire entrer, puisqu'il n'a accès ni à Home Assistant ni à la tablette.
#    Il pouvait ranger sa maison, pas y ajouter une pièce.
#
# ⚠️ ET ON NE LUI DEMANDE PAS QUELLE PILE IL UTILISE. Zigbee2MQTT ou ZHA, c'est
#    une décision d'installateur, prise une fois. Le boîtier regarde les
#    services que Home Assistant expose et en déduit lequel est là.
#
# ⚠️ SOIXANTE SECONDES, ET PAS « OUVERT ». Un réseau Zigbee laissé ouvert
#    accepte n'importe quel appareil du voisinage, et un appareil appairé par
#    erreur se retire à la main, dans une interface que l'hôte n'a pas. La
#    fenêtre se referme d'elle-même : c'est le seul réglage qui ne demande
#    aucune vigilance.
# =====================================================================
DUREE_APPAIRAGE_S = 60
SUJET_Z2M = "zigbee2mqtt/bridge/request/permit_join"


def pile_zigbee(ha):
    """« zigbee2mqtt », « zha », ou None si aucune n'est en place."""
    try:
        familles = ha.services()
    except Exception:
        return None
    dispo = {}
    for f in familles or []:
        if isinstance(f, dict) and f.get("domain"):
            dispo[f["domain"]] = set((f.get("services") or {}).keys())

    # Zigbee2MQTT d'abord : quand les deux sont là, c'est lui qui pilote la
    # clé, ZHA ne ferait que répondre à côté.
    if "mqtt" in dispo and "publish" in dispo["mqtt"]:
        try:
            entites = ha.states() or []
        except Exception:
            entites = []
        if any("zigbee2mqtt" in str(e.get("entity_id", "")) for e in entites):
            return "zigbee2mqtt"
    if "zha" in dispo and "permit" in dispo["zha"]:
        return "zha"
    # MQTT présent mais aucun signe de Zigbee2MQTT : le broker sert à autre
    # chose. On ne devine pas.
    return None


def ouvrir_appairage(ha, duree=None):
    """Ouvre l'appairage sur la pile en place. Rend (statut, résultat)."""
    secondes = int(duree or DUREE_APPAIRAGE_S)
    # Une fenêtre longue est une fenêtre oubliée : on borne des deux côtés.
    secondes = max(30, min(secondes, 300))

    pile = pile_zigbee(ha)
    if pile is None:
        return "failed", {"error":
            "aucune passerelle Zigbee sur ce boîtier : la clé n'est pas "
            "branchée, ou son module n'est pas installé"}

    if pile == "zigbee2mqtt":
        ha.call_service("mqtt", "publish", {
            "topic": SUJET_Z2M,
            "payload": json.dumps({"time": secondes}),
        })
    else:
        ha.call_service("zha", "permit", {"duration": secondes})

    try:
        journal_evenement("zigbee", "info",
                          {"operation": "appairage", "pile": pile, "duree_s": secondes})
    except Exception:
        pass
    return "acked", {"pile": pile, "duree_s": secondes}


# =====================================================================
# POSER LA PASSERELLE ZIGBEE — pour que l'hôte n'ait rien à installer
#
# ⛔ « BRANCHEZ LA CLÉ » NE SUFFIT PAS, ET C'EST LE PIÈGE. Une clé Zigbee est
#    un bout de silicium tant que rien ne la pilote : il faut un facteur
#    (Mosquitto) et un traducteur (Zigbee2MQTT), tous deux à installer dans
#    Home Assistant, puis à régler avec le bon port et le bon adaptateur. Or
#    l'hôte n'a pas accès à Home Assistant. Lui demander de le faire, c'est
#    l'envoyer là où on a décidé qu'il n'irait pas.
#
# ⚠️ LE PORT NE SE DEVINE PAS DE MÉMOIRE. `/dev/ttyACM0` change de numéro d'un
#    redémarrage à l'autre, selon ce qui a été branché avant. On demande donc à
#    la machine son chemin stable (`/dev/serial/by-id/…`), celui qui désigne la
#    clé et pas la place qu'elle occupait ce jour-là.
#
# ⚠️ ET L'ADAPTATEUR N'EST PAS LE MÊME SELON LA CLÉ. Le ZBDongle-E porte une
#    puce Silicon Labs et veut `ember` ; le -P, une puce Texas Instruments et
#    veut `zstack`. Le mauvais choix donne une passerelle qui démarre, semble
#    saine, et ne voit jamais un seul appareil.
# =====================================================================
MOSQUITTO = "core_mosquitto"
ZIGBEE2MQTT = "45df7312_zigbee2mqtt"


def _adaptateur_de(chemin):
    """Quelle pile de radio pour cette clé, d'après son nom de port."""
    c = (chemin or "").lower()
    if "silicon_labs" in c or "zbdongle-e" in c or "cp210" in c and "sonoff" in c:
        return "ember"
    if "texas" in c or "zbdongle-p" in c or "cc2652" in c or "slab_usbtouart" in c:
        return "zstack"
    if "conbee" in c or "deconz" in c:
        return "deconz"
    # Inconnue : `ember` est le cas le plus fréquent dans notre kit, et un
    # mauvais choix se voit tout de suite (aucun appareil ne répond).
    return "ember"


def port_zigbee(sup):
    """Le chemin stable de la clé Zigbee branchée, ou None."""
    try:
        infos = (sup.materiel() or {}).get("data") or {}
    except Exception:
        return None
    candidats = []
    for d in infos.get("devices") or []:
        if d.get("subsystem") != "tty":
            continue
        for chemin in d.get("by_id") and [d["by_id"]] or []:
            c = chemin.lower()
            # ⛔ On écarte ce qui n'est pas une radio : un boîtier peut porter
            #    un modem, un onduleur, un lecteur de carte.
            if any(m in c for m in ("zigbee", "zbdongle", "sonoff", "silicon_labs",
                                    "cc2652", "conbee", "slab_usbtouart", "cp2102")):
                candidats.append(chemin)
    return candidats[0] if candidats else None


def poser_passerelle_zigbee(ha, sup):
    """Installe, règle et démarre ce qu'il faut pour appairer du Zigbee."""
    if sup is None:
        return "failed", {"error": "sans Superviseur, aucun module à poser"}

    deja = pile_zigbee(ha)
    if deja:
        return "acked", {"deja": deja, "note": "une passerelle est déjà en place"}

    port = port_zigbee(sup)
    if not port:
        return "failed", {"error":
            "aucune clé Zigbee reconnue sur la machine : vérifiez qu'elle est "
            "branchée, si possible sur une rallonge USB"}

    fait = []
    # 1. Le facteur.
    fait.append(sup.poser_addon(MOSQUITTO))
    sup.demarrer_addon(MOSQUITTO)

    # 2. Le traducteur, réglé sur LA clé trouvée.
    fait.append(sup.poser_addon(ZIGBEE2MQTT))
    adaptateur = _adaptateur_de(port)
    sup.regler_addon(ZIGBEE2MQTT, {
        "data_path": "/config/zigbee2mqtt",
        "serial": {"port": port, "adapter": adaptateur},
    })
    sup.demarrer_addon(ZIGBEE2MQTT)

    # 3. Le lien entre Home Assistant et le facteur. Home Assistant repère le
    #    module tout seul et propose de le brancher : il reste à dire oui.
    lien = _confirmer_mqtt(ha)

    try:
        journal_evenement("zigbee", "info",
                          {"operation": "passerelle", "port": port,
                           "adaptateur": adaptateur, "mqtt": lien})
    except Exception:
        pass
    return "acked", {"port": port, "adaptateur": adaptateur,
                     "modules": fait, "mqtt": lien}


def _confirmer_mqtt(ha, essais=6):
    """Accepte la proposition de branchement MQTT de Home Assistant.

    ⚠️ ELLE N'ARRIVE PAS TOUT DE SUITE. Home Assistant repère le module une
       fois qu'il tourne : on regarde plusieurs fois plutôt qu'une, sinon on
       conclut « pas de proposition » alors qu'elle arrive deux secondes après.
    """
    for _ in range(essais):
        try:
            flux = ha._req("GET", "/api/config/config_entries/flow") or []
        except Exception:
            flux = []
        for f in flux if isinstance(flux, list) else []:
            if f.get("handler") == "mqtt":
                try:
                    ha._req("POST", "/api/config/config_entries/flow/%s" % f.get("flow_id"), {})
                    return "branché"
                except Exception as e:
                    return "à confirmer dans Home Assistant (%s)" % str(e)[:60]
        time.sleep(5)
    return "aucune proposition vue — à brancher à la main si l'appairage échoue"


def dispatch(cmd, ha, store, sup=None, version_ha=None, differes=None):
    t = cmd.get("type")
    p = cmd.get("payload") or {}

    if t == "hub.ping":
        return "acked", {"agent_version": AGENT_VERSION, "ha_version": version_ha}

    if t == "hub.file.put":
        return "acked", store.put(p["path"], p["content"])

    if t == "hub.file.delete":
        return "acked", store.delete(p["path"])

    if t == "hub.apply":
        # LE bon grain de mise à jour : écrire N fichiers → valider → recharger,
        # ATOMIQUEMENT. Config invalide → rollback total : zéro trace sur le disque,
        # le hub garde exactement ce qui marchait avant. C'est ça, le fail-safe.
        files = p.get("files", [])
        reload_domains = p.get("reload", [])

        # ⛔ NE JAMAIS RÉPONDRE « APPLIQUÉ » POUR UN FICHIER QUE PERSONNE NE LIT.
        for f in files:
            chemin = str(f.get("path") or "")
            dossier = chemin.split("/")[0] + "/" if "/" in chemin else chemin
            lu = configuration_lit(store.root, dossier)
            if lu is False:
                return "failed", {"error":
                    "« %s » n'est pas inclus dans la configuration de ce boîtier : "
                    "le fichier serait écrit et jamais lu. Posez l'inclusion "
                    "(atelier-hub.mjs le fait depuis le 03/08/2026)." % dossier}
        for d in reload_domains:
            if d not in DOMAINES_RECHARGEABLES:
                return "failed", {"error": "domaine non rechargeable : " + str(d)}

        # CONTRAT DE COMPATIBILITÉ — Home Assistant sort une version cassante
        # par mois. Une recette peut exiger une version d'agent ou de cœur ;
        # si on ne l'a pas, on REFUSE PROPREMENT (rien n'est écrit, rien n'est
        # rechargé) au lieu de casser la config du hub. La dérive le montre,
        # et la réconciliation reviendra quand le hub sera à jour.
        if not version_au_moins(AGENT_VERSION, p.get("min_agent_version")):
            return "failed", {"raison": "incompatible",
                              "detail": "agent %s, requis >= %s" % (AGENT_VERSION, p.get("min_agent_version"))}
        if p.get("min_ha_version") and not version_au_moins(version_ha, p.get("min_ha_version")):
            return "failed", {"raison": "incompatible",
                              "detail": "Home Assistant %s, requis >= %s" % (version_ha or "inconnue", p.get("min_ha_version"))}
        snapshot = {f["path"]: store.read(f["path"]) for f in files}
        try:
            for f in files:
                store.put(f["path"], f["content"])
            chk = ha.check_config()
            if chk.get("result") != "valid":
                _rollback(store, snapshot)
                return "failed", {"refused": "check_config invalide", "errors": chk.get("errors")}
            for d in reload_domains:
                ha.reload(d)
            return "acked", {"applied": [f["path"] for f in files], "reloaded": reload_domains}
        except Exception as e:
            _rollback(store, snapshot)
            return "failed", {"error": "apply annulé (rollback) : " + str(e)}

    if t == "hub.config.check":
        r = ha.check_config()
        ok = r.get("result") == "valid"
        return ("acked" if ok else "failed"), r

    if t == "hub.reload":
        domain = p.get("domain", "automation")
        if domain not in DOMAINES_RECHARGEABLES:
            return "failed", {"error": "domaine non rechargeable : " + str(domain)}
        # FAIL-SAFE : on valide AVANT de recharger. Config invalide → on refuse,
        # le hub garde son ancienne config qui, elle, fonctionnait.
        chk = ha.check_config()
        if chk.get("result") != "valid":
            return "failed", {"refused": "check_config invalide", "errors": chk.get("errors")}
        ha.reload(domain)
        return "acked", {"reloaded": domain}

    if t == "hub.service":
        # ⛔ LA LISTE BLANCHE D'ABORD. Cette ligne acceptait n'importe quel
        #    domaine — son commentaire donnait même la serrure en exemple.
        #    Voir SERVICES_AUTORISES pour le raisonnement.
        domaine = str(p.get("domain") or "")
        service = str(p.get("service") or "")
        if not service_autorise(domaine, service, p.get("data")):
            return "failed", {"error":
                "service refusé par le boîtier : %s.%s" % (domaine, service)}
        ha.call_service(domaine, service, p.get("data"))
        return "acked", {"called": domaine + "." + service}

    if t == "hub.zigbee.installer":
        pret, raison = ha_pret(ha)
        if not pret:
            return "failed", {"error": raison}
        return poser_passerelle_zigbee(ha, sup)

    if t == "hub.zigbee.appairage":
        pret, raison = ha_pret(ha)
        if not pret:
            return "failed", {"error": raison}
        return ouvrir_appairage(ha, p.get("duree_s"))

    if t == "hub.inventaire":
        # « Qu'est-ce qu'il y a dans ce logement ? » — la réponse part dans
        # l'accusé, et hub-sync la recopie dans la table `inventaire`.
        pret, raison = ha_pret(ha)
        if not pret:
            return "failed", {"error": raison}
        return "acked", inventaire(ha, sup)

    # -----------------------------------------------------------------
    # ENTRETIEN DE LA MACHINE — tout ce qui suit passe par le Superviseur.
    # Sans lui (add-on lancé hors HA, ou hassio_api désactivé), on le dit
    # franchement plutôt que d'échouer de façon obscure.
    # -----------------------------------------------------------------
    if t.startswith("hub.addon.") or t.startswith("hub.core.") \
       or t.startswith("hub.backup.") or t.startswith("hub.host."):
        if sup is None:
            return "failed", {"error": "Superviseur indisponible — l'add-on doit tourner "
                                       "sur un hub Home Assistant avec hassio_api"}

    if t == "hub.addon.update":
        # slug = 'self' ⇒ l'agent se met à jour LUI-MÊME. Le Superviseur va le
        # stopper : il ne pourra jamais acquitter après coup. D'où l'accusé
        # d'abord. Sa nouvelle version sera annoncée au redémarrage (boot).
        slug = p.get("slug") or "self"
        version = p.get("version")
        return _differer(differes, "addon.update:" + slug,
                         lambda: sup.maj_addon(slug, version, ha),
                         "mise à jour de l'add-on %s vers %s" % (slug, version or "la version installée"),
                         cible={"version": version} if version else None)

    if t == "hub.core.update":
        version = p.get("version")
        if not version:
            return "failed", {"error": "version cible obligatoire — on ne met jamais à jour « au dernier »"}
        return _differer(differes, "core.update",
                         lambda: sup.maj_core(version),
                         "mise à jour de Home Assistant vers " + str(version))

    if t == "hub.core.restart":
        # ⛔ ON REGARDE LA CONFIGURATION AVANT DE COUPER.
        #
        # Home Assistant relit tout au démarrage : une configuration invalide
        # ne se voit pas tant qu'il tourne, et se paie au redémarrage — il ne
        # revient pas. Le boîtier, lui, reste joignable (notre add-on et le
        # Superviseur survivent), donc on peut encore réparer à distance ; mais
        # entre-temps le logement n'a plus de domotique, et l'écran du voyageur
        # n'a plus rien à afficher.
        #
        # Le contrôle existe et ne coûte rien : on refuse plutôt que d'éteindre.
        # Si Home Assistant ne répond pas du tout, on redémarre quand même —
        # c'est précisément le cas où le redémarrage est le remède.
        verdict = None
        try:
            verdict = ha.check_config()
        except Exception:
            verdict = None
        if isinstance(verdict, dict) and verdict.get("result") == "invalid":
            return "failed", {
                "error": "configuration invalide : Home Assistant ne redémarrerait pas",
                "detail": str(verdict.get("errors") or "")[:400],
            }
        return _differer(differes, "core.restart",
                         lambda: sup.redemarrer_core(), "redémarrage de Home Assistant")

    if t == "hub.backup.create":
        nom = p.get("nom") or ("brightstay-" + _now_iso())
        mdp = p.get("mot_de_passe") or os.environ.get("BS_BACKUP_PASSWORD") or None
        return _differer(differes, "backup.create",
                         lambda: sup.creer_sauvegarde(nom, mdp),
                         "sauvegarde complète « %s »" % nom)

    if t == "hub.backup.list":
        return "acked", sup.liste_sauvegardes()

    if t == "hub.host.reboot":
        return _differer(differes, "host.reboot",
                         lambda: sup.redemarrer_hote(), "redémarrage du hub")

    if t == "hub.pad.commande":
        # Le geste de réparation décidé par le serveur : recharger la bonne
        # page, rallumer l'écran, remettre un réglage qui a dérivé…
        cmdp = p.get("cmd")
        if cmdp not in PAD_COMMANDES_AUTORISEES:
            return "failed", {"error": "commande de pad non autorisée : " + str(cmdp)}
        pad = _pad(p.get("mot_de_passe"))
        if pad is None:
            return "failed", {"error": "pad introuvable sur le réseau local"}
        params = {k: v for k, v in (p.get("params") or {}).items()}
        # Fully attend « true »/« false » en texte pour les interrupteurs
        for k, v in list(params.items()):
            if isinstance(v, bool):
                params[k] = "true" if v else "false"
        # On retient tout réglage qu'on nous demande de poser, pour le
        # rapporter désormais : sans ça, le serveur le redemanderait à
        # chaque tour, indéfiniment (défaut vu au Raspberry le 27/07).
        if cmdp in ("setStringSetting", "setBooleanSetting"):
            _apprendre_reglage(params.get("key"))
            # « {hub} » → notre adresse, vue depuis la tablette elle-même
            if "value" in params:
                params["value"] = _substituer_hub(
                    params["value"], adresse_vue_depuis(pad.ip))
        return "acked", {"pad": pad.ip, "cmd": cmdp, "reponse": pad.commande(cmdp, **params)}

    if t == "hub.pad.deploy":
        # On envoie une RÉFÉRENCE, pas 14 Mo dans une commande : le hub va
        # chercher le paquet et vérifie son empreinte avant tout déballage.
        # Sans couche : l'ancien paquet unique. Un boîtier d'avant reçoit donc
        # exactement ce qu'il recevait.
        # L'adresse se DEMANDE, elle ne se lit plus dans la commande : le seau
        # est privé. En cas d'échec on retombe sur celle de la commande —
        # c'est le chemin des boîtiers d'avant, et le filet pendant la bascule.
        adresse = adresse_signee_du_paquet(p.get("version")) or p.get("url")
        resultat = deployer_pad(p.get("version"), adresse, p.get("sha256"),
                                p.get("couche") or "complet")
        # Déployer ne suffit pas : la tablette affiche toujours l'ancienne
        # page tant que personne ne la recharge (constaté au Raspberry).
        marquer_pad_a_rafraichir(p.get("version"))
        return "acked", resultat

    if t == "hub.pad.identite":
        # Le serveur confie au hub de quoi parler à SA tablette. Envoyé une
        # fois, gardé ensuite — y compris après un remplacement de hub.
        return "acked", enregistrer_acces_pad(
            p.get("mot_de_passe"), p.get("ip"), p.get("code_maintenance"))

    if t == "hub.pad.config":
        # Marque, nom, pièces, options : tout ce qui distingue un client d'un
        # autre sans qu'on ait à fabriquer une autre interface.
        return "acked", ecrire_config_pad(p.get("config") or {}, p.get("version"))

    if t == "hub.pad.rollback":
        # Les versions précédentes sont restées sur le disque : revenir en
        # arrière ne demande aucun réseau. C'est le point important — on peut
        # réparer une mauvaise interface même si le cloud est injoignable.
        return "acked", revenir_pad(p.get("version"), p.get("couche") or "complet")

    if t == "hub.versions":
        # « qui tourne sur quoi » — la base de la matrice de compatibilité.
        core = sup.info_core() or {}
        addons = (sup.info_addons() or {}).get("addons", [])
        return "acked", {
            "agent": AGENT_VERSION,
            "core": core.get("version"),
            "core_disponible": core.get("version_latest"),
            "addons": {a.get("slug"): a.get("version") for a in addons},
        }

    if t == "hub.mesures":
        # Le serveur dit au hub s'il doit tenir une courbe fine. Voir
        # `mesures_fines()` : c'est le palier du logement qui décide.
        return "acked", poser_mesures_fines(bool(p.get("fines")))

    if t == "hub.logs":
        return lire_journal(sup, p.get("source"), p.get("lignes"))

    return "failed", {"error": "type de commande inconnu : " + str(t)}


# =====================================================================
# LIRE UN JOURNAL — et n'en laisser sortir aucun secret.
# =====================================================================

# Ce qu'on accepte de lire. ⛔ PAS DE CHEMIN LIBRE : le nom vient du serveur,
# et il finit dans une adresse du Superviseur. Une liste fermée, et rien d'autre.
SOURCES_JOURNAL = {
    "agent": "/addons/self/logs",
    "core": "/core/logs",
    "superviseur": "/supervisor/logs",
    "machine": "/host/logs",
}

# ⛔ CE QUE LES JOURNAUX DE HOME ASSISTANT CONTIENNENT VRAIMENT.
#    Des jetons de longue durée, le MOT DE PASSE DE LA TABLETTE, des clés
#    d'API, le nom du Wi-Fi du logement, parfois des noms de voyageurs. Le
#    journal des commandes ne rend déjà le contenu que pour une liste blanche,
#    précisément pour ça. Diffuser un journal brut dans un navigateur défait
#    cette précaution en une ligne — on caviarde donc AVANT l'envoi, jamais
#    après : ce qui n'est pas parti ne peut pas fuir.
_MASQUES = [
    # Jetons JWT (Home Assistant, Supabase) — trois blocs base64 séparés de points.
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "«jeton»"),
    # « token=... », « password: ... », « api_key ... » et leurs variantes.
    # ⛔ LE MOT-CLÉ PEUT ÊTRE COLLÉ À UN AUTRE, ET C'EST LE CAS QUI COMPTE.
    #    Première version : `\bpassword\b`. Elle laissait passer
    #    `remoteAdminPassword=…` — c'est-à-dire EXACTEMENT le mot de passe de
    #    la tablette, celui que les journaux de l'agent écrivent. Un contrôle
    #    l'a prise en défaut. On accepte donc un préfixe collé.
    (re.compile(r"(?i)\b([\w.-]*(?:token|password|passwd|mot_de_passe|secret|"
                r"api[_-]?key|apikey|authorization|bearer|hub[_-]?key|"
                r"service[_-]?role))\b\s*[:=]?\s*"
                r"[\"']?([A-Za-z0-9._~+/\-]{8,})[\"']?"), r"\1=«masqué»"),
    # Une longue suite de base64 isolée : presque toujours une clé.
    (re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"), "«masqué»"),
]


def caviarder(texte):
    """Retirer d'un journal ce qui ne doit jamais atteindre un navigateur."""
    for motif, remplacement in _MASQUES:
        texte = motif.sub(remplacement, texte)
    return texte


def lire_journal(sup, source, lignes=None):
    """Les dernières lignes d'un journal, caviardées.

    ⚠️ RIEN N'EST CONSERVÉ. Le texte transite par le résultat de la commande,
    que le serveur efface dès qu'il l'a servi. C'est le seul chemin possible :
    le hub ne sait parler que par cette file — mais il n'y a aucune raison d'y
    laisser quoi que ce soit.
    """
    if sup is None:
        return "failed", {"error": "Superviseur indisponible — l'add-on doit tourner "
                                   "sur une machine Home Assistant OS."}
    chemin = SOURCES_JOURNAL.get(str(source or "agent"))
    if not chemin:
        return "failed", {"error": "source inconnue : %s (attendu : %s)"
                                   % (source, ", ".join(sorted(SOURCES_JOURNAL)))}
    try:
        brut = sup.texte(chemin, timeout=30)
    except Exception as e:
        return "failed", {"error": "journal illisible : %s" % str(e)[:160]}

    if isinstance(brut, (bytes, bytearray)):
        brut = brut.decode("utf-8", "replace")
    brut = str(brut or "")

    try:
        n = max(20, min(int(lignes or 200), 500))
    except (TypeError, ValueError):
        n = 200
    dernieres = brut.rstrip("\n").split("\n")[-n:]
    texte = caviarder("\n".join(dernieres))

    # ⚠️ Un plafond dur : un journal de Home Assistant peut peser plusieurs
    #    mégaoctets, et il passerait dans une file faite pour des ordres.
    if len(texte) > 120_000:
        texte = "…(début coupé)…\n" + texte[-120_000:]

    return "acked", {
        "source": source or "agent",
        "lignes": len(dernieres),
        "texte": texte,
        "lu_a": _now_iso(),
    }


# =====================================================================
# Un tour de synchronisation avec hub-sync (le contact EST le heartbeat).
# =====================================================================
def sync_once(hub_url, hub_key, events=None, acks=None, timeout=20):
    body = json.dumps({"events": events or [], "acks": acks or []}).encode()
    req = urllib.request.Request(hub_url, data=body, method="POST")
    req.add_header("x-hub-key", hub_key)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def adresse_signee_du_paquet(version, timeout=20):
    """La clé du boîtier échangée contre une adresse de téléchargement.

    POURQUOI ON NE PREND PLUS L'ADRESSE DANS LA COMMANDE. Elle pointait sur un
    seau public : n'importe qui la connaissant téléchargeait l'écran d'un
    client, sa marque comprise. Le seau est privé désormais, et l'adresse se
    demande — contre la clé de CE boîtier, qui ne donne accès qu'aux versions
    que sa propre fiche nomme.

    ⚠️ ON REND None PLUTÔT QUE DE LEVER. L'appelant retombe alors sur l'adresse
    de la commande. C'est ce qui fait qu'un boîtier continue de se mettre à
    jour pendant la transition, et qu'un incident sur cette fonction ne fige
    pas le parc : au pire on repasse par l'ancien chemin.
    """
    base = os.environ.get("BS_HUB_SYNC_URL", "")
    cle = os.environ.get("BS_HUB_KEY", "")
    if not (base and cle and version):
        return None
    # Même projet, même racine : `…/functions/v1/hub-sync` → `…/functions/v1/paquet`.
    url = base.rsplit("/", 1)[0] + "/paquet"
    body = json.dumps({"version": version}).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("x-hub-key", cle)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            rep = json.loads(r.read().decode())
        return rep.get("url") or None
    except Exception as e:
        # On le DIT : un boîtier qui retombe silencieusement sur l'ancienne
        # adresse donnerait l'illusion que le seau privé fonctionne.
        print("[hub-agent] adresse de paquet non obtenue pour", version, ":", e, flush=True)
        return None


def traiter(commands, ha, store, sup=None, version_ha=None, differes=None):
    """Exécute une salve de commandes, rend la liste d'acks à renvoyer.

    `differes` (liste facultative) recueille les opérations longues à lancer
    APRÈS l'envoi des accusés — cf. _differer()."""
    acks = []
    for cmd in commands:
        try:
            status, result = dispatch(cmd, ha, store, sup, version_ha, differes)
        except Exception as e:                       # crash-only : on isole l'échec
            status, result = "failed", {"error": str(e)}
        acks.append({"command_id": cmd["id"], "status": status, "result": result})
    return acks


# Appareils dont la DISPARITION est en soi une information grave : si un
# détecteur de fumée décroche du réseau, l'hôte se croit protégé alors qu'il ne
# l'est plus — et personne ne le sait aujourd'hui. C'est le défaut le plus lourd
# pour un produit de sécurité.
CLASSES_SECURITE = {"smoke", "gas", "carbon_monoxide", "moisture"}
MAX_LISTE = 15          # un événement porte des FAITS, pas un dump (hub-sync plafonne à 8 Ko)


# =====================================================================
# L'INVENTAIRE — dire au serveur ce que le hub voit chez l'hôte.
#
# POURQUOI. Les pièces d'un logement venaient des « zones » de Home
# Assistant, et le rangement des appareils se faisait sur la tablette,
# derrière un code installateur. Or l'hôte n'aura JAMAIS accès à Home
# Assistant : il ne peut ni créer une pièce, ni corriger un nom. Le
# rangement remonte donc dans l'espace Brightstay — mais pour ranger, il
# faut d'abord savoir ce qu'il y a. C'est ce que fait cette commande.
#
# On lit `/api/states` en REST : les registres (zones, appareils) ne sont
# accessibles qu'en WebSocket, et nous n'en avons pas besoin puisque les
# pièces ne viennent PLUS de Home Assistant. Zéro dépendance ajoutée.
#
# On renvoie des FAITS COMPACTS, pas un dump : un état complet porte des
# dizaines d'attributs inutiles ici, et l'inventaire d'un grand logement
# passerait la limite de taille.
# =====================================================================
DOMAINES_INVENTAIRE = {"light", "cover", "climate", "media_player", "lock", "fan", "switch"}
# Un capteur binaire n'entre que s'il sert : sécurité, ouvrant, ou secteur.
# Tout le reste (mouvement, mise à jour, connectivité…) est du bruit à l'écran.
CLASSES_BINAIRES_UTILES = CLASSES_SECURITE | {"window", "opening", "garage_door", "door", "power"}
MAX_INVENTAIRE = 400    # au-delà, on tronque ET on le dit (jamais de coupe silencieuse)


def ha_pret(ha):
    """« Home Assistant a-t-il fini de démarrer ? » — (prêt, raison).

    ⚠️ Pendant son démarrage, Home Assistant RÉPOND DÉJÀ, mais avec une liste
    PARTIELLE : ses intégrations arrivent les unes après les autres. Un inventaire
    pris à ce moment-là remplaçait la liste complète par une liste amputée, et
    l'hôte voyait ses appareils disparaître sans raison. Mieux vaut refuser : la
    lecture sera redemandée quelques secondes plus tard."""
    try:
        etat = (ha.config() or {}).get("state")
    except Exception:
        return True, None          # illisible : on ne bloque pas sur un doute
    if etat and etat != "RUNNING":
        return False, "Home Assistant démarre encore (%s)" % etat
    return True, None


def _slug_ha(nom):
    """Le nom d'entité que Home Assistant fabrique à partir d'un libellé.

    « Brightstay Hub Agent » → « brightstay_hub_agent ». C'est la règle de Home
    Assistant : minuscules, tout ce qui n'est ni lettre ni chiffre devient un
    tiret bas, pas de doublons ni de bords."""
    out = []
    for c in str(nom or "").lower():
        out.append(c if (c.isalnum() and ord(c) < 128) else "_")
    return "_".join(x for x in "".join(out).split("_") if x)


def entites_de_nos_modules(sup):
    """Les interrupteurs que Home Assistant crée pour NOS PROPRES modules.

    ⛔ ILS N'ONT RIEN À FAIRE SUR LA TABLETTE DU VOYAGEUR. Home Assistant crée
    un interrupteur par module installé — dont le nôtre. Rangé dans une pièce,
    il s'afficherait au mur comme « Brightstay Hub Agent », à côté des lampes.
    Un voyageur curieux l'éteint : plus d'agent, donc plus de surveillance,
    plus de mise à jour, plus de réparation à distance. Depuis son canapé, et
    sans mauvaise intention.

    Constaté le 02/08/2026 sur un boîtier réel : `switch.brightstay_hub_agent`
    et `switch.matter_server` remontaient comme des appareils du logement.

    On les nomme à partir de la liste du Superviseur plutôt que d'une liste
    écrite à la main : un module ajouté demain sera écarté sans qu'on y pense.
    """
    if sup is None:
        return set()
    try:
        liste = (sup.info_addons() or {}).get("addons", []) or []
    except Exception:
        return set()
    interdits = set()
    for a in liste:
        for base in (a.get("name"), a.get("slug")):
            slug = _slug_ha(base)
            if not slug:
                continue
            # Home Assistant expose un interrupteur, et parfois une mise à jour.
            interdits.add("switch." + slug)
            interdits.add("update." + slug + "_update")
    return interdits


def inventaire(ha, sup=None):
    """Ce que le hub voit, compacté pour l'écran de configuration."""
    etats = ha.states() or []
    appareils = []
    # Nos propres modules ne sont pas des appareils du logement.
    nous = entites_de_nos_modules(sup)

    for e in etats:
        eid = e.get("entity_id") or ""
        if eid in nous:
            continue
        domaine = eid.split(".")[0] if "." in eid else ""
        attrs = e.get("attributes") or {}
        classe = attrs.get("device_class")

        if domaine == "binary_sensor":
            if classe not in CLASSES_BINAIRES_UTILES:
                continue
        elif domaine not in DOMAINES_INVENTAIRE:
            continue

        # ⚠️ PAS DE FILTRE « ENTITÉ CACHÉE » ICI, et c'est délibéré. « Cachée » et
        # « désactivée » vivent dans le REGISTRE de Home Assistant, pas dans l'état
        # d'une entité : `/api/states` ne les porte tout simplement pas. Le filtre
        # qui les cherchait dans les attributs ne pouvait jamais se déclencher — il
        # promettait un tri qui n'avait pas lieu. Les registres, eux, ne sont
        # accessibles qu'en WebSocket, ce que cet agent ne fait pas (et qu'on ne
        # veut pas ajouter pour ça). Si le bruit devient réel sur le terrain, on le
        # traitera côté app, où l'hôte peut mettre un appareil de côté d'un clic.

        etat = e.get("state")
        a = {
            "id": eid,
            "domaine": domaine,
            # Sans nom convivial, on renvoie l'identifiant : l'app le repère
            # comme technique et demande à l'hôte de le nommer.
            "nom": attrs.get("friendly_name") or eid,
            "disponible": etat not in (None, "unavailable", "unknown"),
        }
        if classe:
            a["classe"] = str(classe)

        if domaine == "climate":
            modes = attrs.get("hvac_modes")
            if isinstance(modes, list):
                # C'est « sait-il refroidir ? » qui distingue une clim d'un radiateur.
                a["modes"] = [str(m) for m in modes][:12]

        if domaine == "light":
            couleurs = attrs.get("supported_color_modes")
            if isinstance(couleurs, list):
                a["couleurs"] = [str(c) for c in couleurs][:8]
            for src, dst in (("min_color_temp_kelvin", "minK"), ("max_color_temp_kelvin", "maxK")):
                v = attrs.get(src)
                if isinstance(v, (int, float)):
                    a[dst] = int(v)

        if domaine == "fan":
            a["vitesses"] = bool(attrs.get("percentage_step") or attrs.get("preset_modes"))

        appareils.append(a)

    appareils.sort(key=lambda x: x["id"])
    total = len(appareils)
    return {
        "vu_le": _now_iso(),
        "appareils": appareils[:MAX_INVENTAIRE],
        "total": total,
        "tronque": total > MAX_INVENTAIRE,
    }


def _jours_certificat():
    """Combien de jours avant que les pads cessent de faire confiance au hub.

    Le certificat d'un hub dure 825 jours : c'est une panne de flotte
    PROGRAMMÉE, à peu près à la même date pour tout le monde, et chaque
    réparation serait un déplacement. On la regarde donc venir de loin — et on
    la mesure là où elle mord : en ouvrant une vraie connexion, exactement
    comme le fait le pad.

    Mesuré seulement si l'image dorée a posé BS_CERT_HOTE (sur un poste de dev
    sans HTTPS, il n'y a rien à mesurer et c'est normal)."""
    hote = os.environ.get("BS_CERT_HOTE")
    if not hote:
        return None
    port = int(os.environ.get("BS_CERT_PORT", "8123"))
    try:
        import ssl as _ssl
        import tempfile as _tf
        pem = _ssl.get_server_certificate((hote, port), timeout=5)
        with _tf.NamedTemporaryFile("w", suffix=".pem", delete=False) as f:
            f.write(pem)
            chemin = f.name
        try:
            # _test_decode_cert est une fonction interne de CPython, stable
            # depuis des années et la seule voie sans dépendance. Tout est sous
            # try/except : au pire on ne sait pas, on ne casse rien.
            infos = _ssl._ssl._test_decode_cert(chemin)
        finally:
            os.unlink(chemin)
        fin = _ssl.cert_time_to_seconds(infos["notAfter"])
        return int((fin - time.time()) // 86400)
    except Exception:
        return None


MAX_RECETTES = 40       # au-delà, on tronque : un événement reste un fait, pas un dump


def _empreintes_recettes(store):
    """Ce que le hub a VRAIMENT sur son disque, chemin par chemin.

    Sans ça, un hub restauré depuis une vieille sauvegarde paraîtrait à jour :
    la base dirait « appliqué », le disque aurait l'ancienne version, et
    personne ne verrait l'écart. Avec ça, le serveur compare et renvoie
    exactement ce qui manque — le hub se remet à niveau tout seul."""
    empreintes = {}
    for prefixe in CHEMINS_AUTORISES:
        racine = prefixe.rsplit("/", 1)[0] if "/" in prefixe else prefixe
        dossier = os.path.join(store.root, racine)
        if not os.path.isdir(dossier):
            continue
        for base, _dossiers, fichiers in os.walk(dossier):
            for f in fichiers:
                chemin = os.path.join(base, f)
                rel = os.path.relpath(chemin, store.root)
                if not rel.startswith(prefixe):
                    continue
                try:
                    with open(chemin, "rb") as fh:
                        empreintes[rel] = hashlib.sha256(fh.read()).hexdigest()[:12]
                except OSError:
                    pass
    if len(empreintes) <= MAX_RECETTES:
        return empreintes, False
    gardees = sorted(empreintes)[:MAX_RECETTES]
    return {k: empreintes[k] for k in gardees}, True


def _adresse_locale(sup=None):
    """L'adresse du boîtier SUR LE RÉSEAU DU LOGEMENT.

    ⚠️ ÇA A COÛTÉ UNE APRÈS-MIDI. Le 02/08/2026, un boîtier a cessé d'appeler.
    Impossible de le retrouver : `homeassistant.local` ne répond pas (le nom
    dépend d'un service que tout pare-feu fait taire), son ancienne adresse
    était périmée, et balayer le réseau n'a rien donné. On savait qu'il existait
    et rien de plus. Une machine qui appelle sait où elle est : qu'elle le DISE.

    ⛔ ON DEMANDE AU SUPERVISEUR, PAS AU SYSTÈME. Première version de ce code :
    une prise UDP pour lire l'interface choisie — elle a rendu `172.30.33.0`,
    l'adresse du CONTENEUR. Techniquement juste, parfaitement inutile, et pire
    que rien puisqu'elle a l'air d'une réponse. Le Superviseur, lui, voit les
    interfaces de la machine.
    """
    if sup is None:
        return None
    try:
        infos = sup.info_reseau() or {}
    except Exception:
        return None
    candidates = []
    for i in (infos.get("interfaces") or []):
        if not i.get("enabled"):
            continue
        adr = ((i.get("ipv4") or {}).get("address") or [])
        for a in adr:
            ip = str(a).split("/")[0]
            if not ip or ip.startswith("127.") or ip.startswith("172.30."):
                continue
            # Une interface « primaire » est celle qui porte la route par
            # défaut : c'est celle par laquelle on peut le joindre.
            candidates.append((0 if i.get("primary") else 1, ip))
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][1]


def _entites_de_mise_a_jour(ha):
    """Ce que Home Assistant sait mettre à jour, et où il en est.

    ⚠️ Sans cette liste, on DEVINE le nom d'une entité — et une entité inconnue
    passée à Home Assistant est ignorée EN SILENCE. On croit avoir agi, il ne
    se passe rien, et on cherche ailleurs. Trois noms essayés le 02/08, trois
    fois « accepté », zéro effet.

    On se limite à l'essentiel : l'identifiant, le titre, et les deux versions.
    Rien de personnel, rien de secret.
    """
    try:
        etats = ha.states() or []
    except Exception:
        return None
    liste = []
    for e in etats:
        eid = str(e.get("entity_id") or "")
        if not eid.startswith("update."):
            continue
        a = e.get("attributes") or {}
        liste.append({
            "id": eid,
            "titre": str(a.get("title") or a.get("friendly_name") or "")[:60],
            "installee": str(a.get("installed_version") or "")[:24],
            "proposee": str(a.get("latest_version") or "")[:24],
            "en_attente": e.get("state") == "on",
        })
        if len(liste) >= 30:
            break
    return liste


def entite_de_mise_a_jour(ha, fiche):
    """L'entité que Home Assistant expose pour mettre CE module à jour.

    ⚠️ ON NE DEVINE PAS SON NOM, ON LE CHERCHE. Le 02/08/2026 j'ai essayé deux
    noms plausibles depuis le serveur : Home Assistant les a acceptés en
    silence — une entité inconnue dans une commande ne provoque aucune erreur —
    et il ne s'est rien passé. Une heure perdue à croire que ça avait marché.

    On lit donc la liste réelle et on compare au titre du module, que le
    Superviseur nous a donné. Aucune supposition ne survit à une liste.
    """
    titre = str((fiche or {}).get("name") or "").strip().lower()
    installee = str((fiche or {}).get("version") or "")
    if not titre:
        return None
    try:
        etats = ha.states() or []
    except Exception as e:
        print("[hub-agent] entités illisibles :", e, flush=True)
        return None

    replis = None
    for e in etats:
        eid = str(e.get("entity_id") or "")
        if not eid.startswith("update."):
            continue
        a = e.get("attributes") or {}
        # Le titre porté par l'entité est celui du module : c'est le lien sûr.
        if str(a.get("title") or "").strip().lower() == titre:
            return eid
        # À défaut : le nom affiché commence par le titre du module, et la
        # version installée concorde. Deux indices valent mieux qu'un.
        nom = str(a.get("friendly_name") or "").strip().lower()
        if nom.startswith(titre) and (
                not installee or str(a.get("installed_version") or "") == installee):
            replis = replis or eid
    return replis


# Ce qui appartient à Home Assistant lui-même, et jamais au logement.
# ⚠️ On ne liste QUE des familles entières et des préfixes sans ambiguïté :
#    un filtre trop large cacherait un vrai appareil, ce qui serait bien pire
#    que le bruit qu'on supprime. Dans le doute, on compte.
_FAMILLES_INTERNES = ("conversation.", "tts.", "stt.", "person.", "zone.",
                      "todo.", "assist_satellite.")
_PREFIXES_INTERNES = ("update.", "sensor.home_assistant_",
                      "binary_sensor.remote_ui")
# ⛔ CEUX-LÀ SE NOMMENT EN ENTIER, PAS PAR PRÉFIXE.
#    Première version : `sensor.backup_`. Un contrôle l'a prise en défaut avec
#    `sensor.backup_battery_niveau` — une pile de secours, c'est-à-dire un VRAI
#    appareil, et de sécurité. Le filtre l'aurait masquée. Un préfixe est une
#    devinette ; ces quatre entités-là ont un nom fixe, imposé par Home
#    Assistant. On les écrit.
_ENTITES_INTERNES = frozenset((
    "event.backup_automatic_backup",
    "sensor.backup_last_attempted_automatic_backup",
    "sensor.backup_last_successful_automatic_backup",
    "sensor.backup_next_scheduled_automatic_backup",
    "sensor.backup_backup_manager_state",
))


def _plomberie_ha(entity_id):
    """Cette entité est-elle de la mécanique de Home Assistant ?"""
    e = (entity_id or "").lower()
    return (e in _ENTITES_INTERNES
            or e.startswith(_FAMILLES_INTERNES)
            or e.startswith(_PREFIXES_INTERNES))


# ⚠️ LA MESURE FINE EST RÉSERVÉE AU PALIER CANARI, ET C'EST UN CHOIX DE COÛT.
#    Un relevé toutes les trente minutes fait 48 lignes par jour et par boîtier.
#    Sur un parc de cinq cents kits, c'est 24 000 lignes par jour pour une
#    courbe que personne ne regardera. Les boîtiers d'essai, eux, sont là pour
#    être regardés : c'est sur eux qu'on veut le détail.
#    Ailleurs, l'instantané horaire suffit — il porte déjà la température.
#
# ⚠️ TRENTE MINUTES, ET PAS DIX. Le pas était de dix minutes ; sur une semaine
#    de courbe, c'est un millier de points pour une machine qui vit entre 45 et
#    55 °C — trois fois plus de lignes sans un degré d'information en plus. Une
#    machine qui s'emballe met des heures, pas des minutes : un point toutes les
#    demi-heures montre la même pente. Ce qui monte VITE (un ordre qui traîne,
#    un hub muet) est déjà surveillé ailleurs, en continu.
PERIODE_MESURE_S = 1800


def _fichier_mesures():
    return os.path.join(PAD_RACINE, "mesures_fines")


def mesures_fines():
    """Ce hub enregistre-t-il une courbe fine ?

    ⚠️ C'EST LE SERVEUR QUI SAIT, PAS LE HUB. Le palier — atelier, canari,
       early, stable — vit dans la fiche du logement ; l'agent ne l'a jamais su
       et n'a pas à le deviner. Le serveur le lui DIT, par une commande, et il
       le garde sur disque : au redémarrage suivant il s'en souvient, sans avoir
       à redemander.

    ⚠️ ÉTEINT PAR DÉFAUT. 48 relevés par jour et par boîtier : sur cinq cents
       kits, 24 000 lignes quotidiennes pour une courbe que personne n'ouvrira.
       Les kits d'essai sont là pour être regardés ; les autres, pour tourner.
    """
    if os.environ.get("BS_MESURES_FINES", "").lower() in ("1", "true", "on", "oui"):
        return True
    try:
        with open(_fichier_mesures(), encoding="utf-8") as f:
            return f.read().strip() == "1"
    except OSError:
        return False


def poser_mesures_fines(actif):
    """Retenir la consigne du serveur, pour qu'elle survive au redémarrage."""
    try:
        os.makedirs(PAD_RACINE, exist_ok=True)
        with open(_fichier_mesures(), "w", encoding="utf-8") as f:
            f.write("1" if actif else "0")
    except OSError as e:
        print("[hub-agent] consigne de mesure non conservée :", e, flush=True)
    return {"mesures_fines": bool(actif)}


def temperature_machine():
    """La température du processeur, lue sur le système.

    ⚠️ ON LIT LA MACHINE, PAS CE QU'ELLE RACONTE. Home Assistant sait exposer
       cette valeur (intégration « System Monitor »), mais il faudrait l'activer
       sur chaque boîtier — et on perdrait la mesure au moment précis où elle
       sert : quand Home Assistant va mal. Le noyau, lui, l'écrit toujours.

    ⚠️ PLUSIEURS ZONES, ON GARDE LA PLUS CHAUDE. Une machine en expose souvent
       trois ou quatre (processeur, carte, alimentation). Prendre la première
       revient à tirer au sort ; prendre la plus chaude, c'est prendre celle qui
       déclenchera la limitation.

    Un boîtier qui chauffe RALENTIT avant de tomber : c'est un signal précoce,
    au même titre que l'allongement du délai des ordres.
    """
    chaudes = []
    try:
        for zone in sorted(glob.glob("/sys/class/thermal/thermal_zone*/temp")):
            try:
                with open(zone, encoding="utf-8") as f:
                    brut = int(f.read().strip())
            except (OSError, ValueError):
                continue
            # Le noyau écrit des milli-degrés ; certaines cartes des degrés.
            c = brut / 1000.0 if brut > 1000 else float(brut)
            # ⛔ Une zone qui rend 0 ou une valeur absurde n'est pas une mesure.
            #    La taire vaut mieux que d'afficher « 0 °C » sur un écran.
            if 5.0 < c < 120.0:
                chaudes.append(round(c, 1))
    except Exception:
        return None
    return max(chaudes) if chaudes else None


_DERNIERE_MESURE = {"quand": 0.0}


def evenement_temperature(pad_temp=None):
    """Un relevé de température, toutes les trente minutes, sur les kits d'essai.

    ⚠️ SÉPARÉ DE L'INSTANTANÉ, ET C'EST VOULU. L'instantané part à chaque
       contact mais n'est CONSERVÉ qu'une fois par heure (déduplication). Pour
       une courbe, une heure laisse passer la moitié d'une montée en charge.
       Cet événement-ci a sa propre clé, au pas de trente minutes : deux points
       par heure, assez pour voir la pente sans doubler la table.

    Rend `None` quand il n'y a rien à dire : pas de palier canari, pas de
    mesure, ou l'intervalle n'est pas écoulé.
    """
    if not mesures_fines():
        return None
    maintenant = time.time()
    if maintenant - _DERNIERE_MESURE["quand"] < PERIODE_MESURE_S:
        return None
    hub = temperature_machine()
    if hub is None and pad_temp is None:
        return None
    _DERNIERE_MESURE["quand"] = maintenant
    return {
        "type": "temperature",
        "severity": "info",
        "payload": {"hub": hub, "pad": pad_temp},
        "occurred_at": _now_iso(),
        # Une empreinte par demi-heure : deux relevés du même créneau ne font
        # qu'une ligne, même si l'agent redémarre entre-temps. C'est le vrai
        # garde-fou du pas de mesure — le compteur en mémoire, lui, repart à
        # zéro à chaque redémarrage de l'agent.
        "dedup_key": "temp-" + time.strftime("%Y%m%d%H", time.gmtime())
                     + "-%d" % (int(time.gmtime().tm_min / 30)),
    }


def instantane_sante(ha, sup=None, store=None):
    """Ce que le hub VOIT, à chaque contact.

    Avant, le seul signal était « l'agent a appelé ». C'est insuffisant à deux
    titres : ça ne dit pas si Home Assistant répond encore (il peut être figé
    pendant que l'agent, lui, tourne), et ça ne dit rien des appareils. Un
    instantané répond aux deux.

    Chaque partie est isolée : un morceau qui échoue n'emporte pas le reste.
    Un instantané incomplet vaut infiniment mieux que pas d'instantané."""
    snap = {"agent": AGENT_VERSION}

    # L'identité de la machine, si le Superviseur est là. Isolée comme le reste :
    # ne pas la connaître ne doit jamais empêcher le contact.
    if sup is not None:
        try:
            mid = (sup.info() or {}).get("machine_id")
            if mid:
                snap["machine_id"] = str(mid)[:64]
        except Exception:
            pass

    repond, erreur = ha.repond()
    snap["ha_repond"] = repond
    if erreur:
        snap["ha_erreur"] = erreur[:200]

    try:
        etats = ha.states() or []
        indispo, secu, piles, interne = [], [], [], []
        for e in etats:
            attrs = e.get("attributes") or {}
            eid = e.get("entity_id", "")
            valeur = e.get("state")
            classe = attrs.get("device_class")
            if valeur in ("unavailable", "unknown"):
                # ⛔ LA PLOMBERIE DE HOME ASSISTANT N'EST PAS UNE PANNE.
                #    Le 03/08, un hub d'essai affichait « 7 hors ligne » : zéro
                #    appareil du logement. C'étaient l'assistant vocal jamais
                #    activé, la voix de synthèse Google jamais sollicitée, la
                #    « personne » sans téléphone, et les trois capteurs de
                #    sauvegarde automatique qui n'ont rien à dire tant qu'aucune
                #    n'est programmée. Un exploitant qui lit « 7 hors ligne »
                #    part chercher sept pannes qui n'existent pas — et le jour
                #    où un VRAI détecteur se tait, il est noyé dans le même
                #    chiffre. On les compte à part.
                (interne if _plomberie_ha(eid) else indispo).append(eid)
                if classe in CLASSES_SECURITE:
                    secu.append(eid)
            if classe == "battery":
                try:
                    niveau = float(valeur)
                    if niveau < 20:
                        piles.append({"entite": eid, "niveau": niveau})
                except (TypeError, ValueError):
                    pass
        snap["entites"] = len(etats)
        snap["indisponibles"] = len(indispo)
        snap["indisponibles_liste"] = sorted(indispo)[:MAX_LISTE]
        # Comptés, mais à part : ce ne sont pas des appareils du logement.
        snap["interne_muet"] = len(interne)
        snap["interne_muet_liste"] = sorted(interne)[:MAX_LISTE]
        snap["securite_muets"] = sorted(secu)[:MAX_LISTE]
        snap["piles_faibles"] = sorted(piles, key=lambda p: p["niveau"])[:MAX_LISTE]
    except Exception as e:
        snap["entites_erreur"] = str(e)[:200]

    # LA VERSION DU CŒUR — trouvée sur le Raspberry, le 27/07/2026.
    #
    # Elle ne venait QUE du Superviseur. Or un hub en conteneur n'en a pas :
    # sur celui-ci, `core` restait vide en permanence, et personne ne s'en
    # apercevait puisque tout le reste marchait. Conséquence : le garde-fou
    # de compatibilité (« cette recette exige une version au moins X »)
    # n'avait aucune version à comparer — il ne gardait rien.
    #
    # Home Assistant sait pourtant se présenter tout seul : /api/config donne
    # la version ET l'état réel. On demande donc au principal intéressé, et
    # le Superviseur — quand il existe — ne sert plus qu'à dire quelle
    # version est DISPONIBLE, ce que lui seul connaît.
    try:
        conf = ha.config()
        if conf.get("version"):
            snap["core"] = conf["version"]
            snap["ha_etat"] = conf.get("state")
    except Exception as e:
        snap["core_erreur"] = str(e)[:120]

    if sup:
        for nom, lire in (
            ("core", lambda: {"core": (sup.info_core() or {}).get("version"),
                              "core_disponible": (sup.info_core() or {}).get("version_latest")}),
            ("addons", lambda: {"addons": {a.get("slug"): a.get("version")
                                           for a in (sup.info_addons() or {}).get("addons", [])}}),
            ("disque", lambda: _disque(sup.info_host() or {})),
            ("sauvegardes", lambda: _sauvegardes((sup.liste_sauvegardes() or {}).get("backups", []))),
        ):
            try:
                snap.update(lire())
            except Exception as e:
                snap[nom + "_erreur"] = str(e)[:120]

    # ⚠️ AVANT DE SONDER LA TABLETTE, SAVOIR OÙ CHERCHER.
    #    Cet appel était plus bas dans la fonction — donc APRÈS `etat_pad()`,
    #    qui a besoin du réseau du logement pour balayer. Le boîtier apprenait
    #    son adresse une fraction de seconde trop tard, à chaque tour, et la
    #    recherche de la tablette partait toujours sur le réseau de Docker.
    #    L'ordre des lignes était toute la panne.
    adresse_du_hub = None
    try:
        adresse_du_hub = _adresse_locale(sup)
        if adresse_du_hub:
            retenir_reseau_du_logement(adresse_du_hub)
    except Exception as e:
        snap["adresse_locale_erreur"] = str(e)[:120]

    try:
        # on dit si le hub PEUT parler à la tablette — jamais avec quoi
        snap["pad_acces"] = bool(_mdp_pad())
        # ⚠️ Rapporté à part du mot de passe : sans ça, un code ajouté APRÈS
        # coup ne serait jamais envoyé — le serveur ne réémet `hub.pad.identite`
        # que tant qu'il croit le hub démuni, et le mot de passe suffisait à le
        # rassurer.
        snap["pad_maintenance"] = bool(_code_maintenance())
        t = temperature_machine()
        if t is not None:
            snap["temperature"] = t
        # ⚠️ LA MESURE QUI MANQUAIT. Sans elle, impossible de savoir à distance
        #    pourquoi une mise à jour échoue — il a fallu lire le code, deviner,
        #    et essayer trois fois. Un booléen l'aurait dit tout de suite.
        snap["ha_admin"] = getattr(ha, "admin", None) is not None
        snap["mesures_fines"] = mesures_fines()
        pad = etat_pad()
        if pad is not None:
            snap["pad"] = pad
    except Exception as e:
        snap["pad_erreur"] = str(e)[:120]

    try:
        snap["pad_version_servie"] = version_pad_servie()
        # LE compte rendu qui déclenche l'envoi des couches : le serveur
        # n'expédie une couche que s'il sait déjà ce qui est en place.
        snap["pad_couches_servies"] = couches_servies()
        # L'empreinte de la PAGE servie, dans le même vocabulaire que celle
        # que la tablette annonce : c'est la seule paire comparable, donc la
        # seule façon de savoir qu'un pad affiche une interface périmée.
        snap["pad_page_servie"] = version_page_servie()
        snap["pad_versions_disponibles"] = versions_pad_disponibles()
        snap["pad_config_version"] = version_config_pad()
        snap["pad_rafraichissement_du"] = os.path.exists(_fichier_pad_a_rafraichir())
    except Exception as e:
        snap["pad_version_erreur"] = str(e)[:120]

    if store is not None:
        try:
            recettes, tronque = _empreintes_recettes(store)
            snap["recettes"] = recettes
            if tronque:
                snap["recettes_tronquees"] = True
        except Exception as e:
            snap["recettes_erreur"] = str(e)[:120]

    jours = _jours_certificat()
    if jours is not None:
        snap["certificat_jours"] = jours

    # ⚠️ DEUX RENSEIGNEMENTS QUI ONT MANQUÉ LE 02/08/2026, ET CHACUN A COÛTÉ
    #    DES HEURES.
    #
    #    · OÙ EST CETTE MACHINE. Quand elle s'est tue, impossible de la
    #      retrouver : le nom `homeassistant.local` ne répond pas, son ancienne
    #      adresse était périmée, et balayer le réseau n'a rien donné. Une
    #      machine qui appelle sait où elle est — qu'elle le dise.
    #    · CE QU'ELLE SAIT METTRE À JOUR. Sans la liste, on devine des noms
    #      d'entités — et Home Assistant ignore EN SILENCE celles qui n'existent
    #      pas. On croit avoir agi ; rien ne se passe.
    # (l'adresse a été lue plus haut : la sonde de la tablette en dépend)
    if adresse_du_hub:
        snap["adresse_locale"] = adresse_du_hub
    if _RESEAU_LOGEMENT.get("base"):
        # Ce que le boîtier balaie pour retrouver sa tablette. Sans ce
        # renseignement, « aucune tablette trouvée » est indiscernable de
        # « j'ai cherché au mauvais endroit » — c'est ce qui nous a coûté
        # une nuit.
        snap["reseau_balaye"] = _RESEAU_LOGEMENT["base"] + ".0/24"

    try:
        maj = _entites_de_mise_a_jour(ha)
        if maj is not None:
            snap["entites_maj"] = maj
    except Exception as e:
        snap["entites_maj_erreur"] = str(e)[:120]

    return {"type": "sante", "severity": "info", "payload": snap,
            "occurred_at": _now_iso(),
            # une trace par heure dans le journal ; l'état courant, lui, est
            # rafraîchi à CHAQUE contact côté serveur.
            "dedup_key": "sante-" + time.strftime("%Y%m%d%H", time.gmtime())}


def _disque(host):
    libre, total = host.get("disk_free"), host.get("disk_total")
    if libre is None or not total:
        return {}
    return {"disque_utilise_pct": round(100 * (float(total) - float(libre)) / float(total))}


def _sauvegardes(liste):
    dates = sorted([b.get("date") for b in liste if b.get("date")])
    return {"sauvegardes": len(liste), "derniere_sauvegarde": dates[-1] if dates else None}


# Les événements produits par le serveur de la tablette : ils naissent dans un
# autre fil que la boucle de synchronisation, qui les vide à son tour suivant.
_EVENEMENTS_HORS_BOUCLE = []


def journal_evenement(type_, severity, payload):
    _EVENEMENTS_HORS_BOUCLE.append({
        "type": type_, "severity": severity,
        "payload": dict(payload or {}, agent_version=AGENT_VERSION),
        "occurred_at": _now_iso(),
        "dedup_key": "%s-%s" % (type_, _now_iso()),
    })


def _evt_maintenance(phase, quoi, detail=None, cible=None):
    """Une opération d'entretien raconte son histoire en deux temps : elle
    annonce qu'elle commence (avant de couper la parole), puis dit comment
    elle a fini. Sans le « début », un hub qui redémarre pour une mise à jour
    légitime ressemblerait à un hub tombé en panne.

    ⚠️ `cible` DIT SUR QUOI L'OPÉRATION PORTAIT — la version visée, par
    exemple. Sans elle, le garde anti-boucle du serveur ne distingue pas
    « la 0.5.0 a encore échoué » de « on essaie maintenant la 0.5.1 » : il
    retient donc les deux, ou aucune. Le 02/08/2026, l'échec était muet sur ce
    point et 26 ordres sont partis en une minute."""
    return {
        "type": "maintenance",
        "severity": "warning" if phase == "echec" else "info",
        "payload": {"phase": phase, "operation": quoi, "detail": detail or {},
                    "agent_version": AGENT_VERSION,
                    **({"cible": cible} if cible else {})},
        "occurred_at": _now_iso(),
        "dedup_key": "maint-%s-%s-%s" % (phase, quoi, _now_iso()),
    }


# =====================================================================
# Boucle principale — poll + exécute + acquitte, indéfiniment.
# =====================================================================
def main():
    hub_url = os.environ["BS_HUB_SYNC_URL"]          # https://<projet>.supabase.co/functions/v1/hub-sync
    hub_key = os.environ["BS_HUB_KEY"]               # bshub_… (vit dans secrets.yaml du hub)
    ha_url = os.environ.get("HA_URL", "http://supervisor/core")
    ha_token = os.environ.get("HA_TOKEN") or os.environ.get("SUPERVISOR_TOKEN", "")
    # ⚠️ DEUX JETONS, ET SURTOUT PAS LE MÊME.
    #
    # Celui du dessus est celui de L'AGENT : il passe par le Superviseur, qui
    # l'accepte. Celui du dessous est celui de LA TABLETTE : elle attaque Home
    # Assistant en direct, depuis le réseau de la maison, et Home Assistant
    # n'accepte que ses propres jetons.
    #
    # Les confondre — ce qui était le cas — donnait une tablette qui affichait
    # tout et ne commandait rien.
    ha_token_pad = os.environ.get("BS_PAD_HA_TOKEN", "")
    config_dir = os.environ.get("HA_CONFIG_DIR", "/homeassistant")
    intervalle = int(os.environ.get("BS_SYNC_INTERVAL", "300"))

    ha = HA(ha_url, ha_token)
    # Le même Home Assistant, mais vu par un compte administrateur. Sert aux
    # gestes qu'un module n'a pas le droit de faire sur lui-même.
    # ⛔ LE JETON D'UN UTILISATEUR NE VA PAS À L'ADRESSE DU SUPERVISEUR.
    #    `ha_url` vaut « http://supervisor/core » : c'est le relais du
    #    Superviseur, et il n'accepte QUE le jeton du Superviseur. Y envoyer un
    #    jeton de compte administrateur donne un 403 — un code nu, sans un mot
    #    d'explication.
    #
    #    Le 03 et le 04/08/2026, la mise à jour de l'agent a échoué SIX FOIS de
    #    suite avec ce 403, alors que le jeton était bien posé et que
    #    l'instantané affichait « ha_admin : oui ». On a cherché du côté des
    #    droits, de la boutique, des règles du Superviseur. C'était l'adresse.
    #
    # ⚠️ C'EST L'ERREUR SYMÉTRIQUE DE CELLE DU 29/07, ET LE CODE LA DÉCRIT DÉJÀ
    #    vingt lignes plus haut, pour la tablette : « le SUPERVISOR_TOKEN ouvre
    #    le Superviseur ; Home Assistant, lui, le refuse. » On l'avait comprise
    #    dans un sens, jamais dans l'autre.
    #
    #    Home Assistant s'atteint en direct : `homeassistant:8123` sur le réseau
    #    des modules, `172.30.32.1:8123` en repli. Et on VÉRIFIE la porte avant
    #    de s'en servir, plutôt que de découvrir son refus au pire moment.
    ha.admin = None
    if ha_token_pad and ha_token_pad != ha_token:
        for direct in ("http://homeassistant:8123", "http://172.30.32.1:8123"):
            essai = HA(direct, ha_token_pad)
            try:
                if essai.states():
                    ha.admin = essai
                    print("[hub-agent] compte administrateur joignable sur", direct, flush=True)
                    break
            except Exception as e:
                print("[hub-agent] %s refuse le jeton admin : %s" % (direct, str(e)[:90]), flush=True)
        if ha.admin is None:
            print("[hub-agent] AUCUNE porte n'accepte le jeton d'administrateur — "
                  "la mise à jour de l'agent échouera", flush=True)
    store = Store(config_dir)
    # Avant tout : que nos dossiers soient LUS. Sans ça, chaque `hub.apply`
    # écrira dans le vide en répondant « appliqué ».
    try:
        assurer_inclusions(config_dir, ha)
    except Exception as e:
        print("[hub-agent] inclusions non vérifiées :", e, flush=True)

    _charger_reglages_appris()   # sinon la boucle des réglages rouvre à chaque relance

    # Le Superviseur n'existe que sur un vrai hub Home Assistant. Sans lui,
    # l'agent garde toutes ses fonctions de recettes et refuse proprement les
    # commandes d'entretien — il ne plante pas.
    # Le hub sert la page du pad dès le démarrage : c'est ce qui rend le
    # logement autonome. Un échec ici n'empêche pas le reste de tourner —
    # mieux vaut un hub qui entretient ses recettes sans servir la page qu'un
    # hub muet.
    # Le dire au démarrage, en clair : sans ce jeton la tablette affichera son
    # écran et aucun bouton ne marchera. C'est exactement la panne qu'on a mis
    # une matinée à comprendre, faute d'une ligne dans le journal.
    if not _jeton_pour_la_tablette(ha_token_pad):
        print("[hub-agent] ⚠ aucun jeton Home Assistant pour la tablette "
              "(option « ha_token » de l'add-on). La page sera servie, mais "
              "elle ne pourra RIEN commander : elle affichera « hub non "
              "connecté ». Créez un jeton de longue durée dans le profil "
              "Home Assistant et collez-le dans les options de l'add-on.",
              flush=True)
    jeton_sup = os.environ.get("SUPERVISOR_TOKEN")
    sup = Supervisor(jeton_sup, os.environ.get("BS_SUPERVISOR_URL", "http://supervisor")) \
        if jeton_sup else None
    # La porte de secours, posée au démarrage. On cherche l'entité plutôt que
    # de deviner son nom — une entité inconnue est acceptée EN SILENCE par Home
    # Assistant, et il ne se passe rien (leçon du 02/08).
    try:
        if sup is not None:
            fiche_moi = sup._req("GET", "/addons/self/info") or {}
            entite_maj = entite_de_mise_a_jour(getattr(ha, "admin", None) or ha, fiche_moi)
            if entite_maj:
                assurer_script_secours(config_dir, entite_maj)
            else:
                print("[hub-agent] entité de mise à jour introuvable : pas de "
                      "script de secours (le boîtier restera dépendant du "
                      "chemin normal)", flush=True)
    except Exception as e:
        print("[hub-agent] script de secours non vérifié :", e, flush=True)
    if sup is None:
        print("[hub-agent] pas de Superviseur : entretien du hub indisponible", flush=True)

    # ⛔ APRÈS `sup`, ET C'EST TOUT LE PROBLÈME.
    #
    #    Ce démarrage était placé QUINZE LIGNES PLUS HAUT, avant que `sup`
    #    existe. Python levait donc « cannot access local variable 'sup' », le
    #    `except` l'avalait poliment, et l'agent continuait comme si de rien
    #    n'était. Résultat : le serveur de l'écran ne démarrait JAMAIS. Le
    #    voyageur n'avait pas de page — pas une mauvaise page, pas une page
    #    vide : rien du tout, le port 8099 ne répondait pas.
    #
    #    Le message « serveur de page KO » partait bien dans le journal de
    #    l'add-on… que personne ne lit. Il est même passé sous mes yeux dans la
    #    sortie des contrôles, et je ne l'ai pas relevé.
    #
    #    ⚠️ NE JAMAIS REMONTER CET APPEL. Il a besoin du Superviseur pour servir
    #    le socle embarqué et rendre compte des couches.
    try:
        demarrer_serveur_pad(ha_url, ha_token_pad, ha=ha, sup=sup)
    except Exception as e:
        # Un écran mort est ce que le voyageur voit EN PREMIER : on le crie.
        print("[hub-agent] ⛔ SERVEUR DE PAGE KO — le voyageur n'aura AUCUN "
              "écran :", e, flush=True)
        # ⚠️ Pas d'événement ici : le journal des événements n'existe pas encore
        #    à cet instant. Le référencer aurait reproduit MOT POUR MOT le
        #    défaut qu'on corrige — un nom utilisé avant d'exister, avalé par un
        #    `except`. L'état est rapporté au premier instantané de santé.

    # Un seul numéro de version pour l'agent : celui de l'add-on installé. Le
    # Superviseur le connaît, on le lui demande plutôt que d'entretenir à la
    # main une constante qui finit toujours par retarder d'une version.
    global AGENT_VERSION
    if sup is not None:
        try:
            version_addon = (sup.info_self() or {}).get("version")
            if version_addon:
                AGENT_VERSION = version_addon
        except Exception as e:
            print("[hub-agent] version de l'add-on illisible, on garde", AGENT_VERSION, ":", e,
                  flush=True)
    print("[hub-agent] version", AGENT_VERSION, flush=True)

    # signale sa présence + sa version au démarrage (atterrit dans `evenements`)
    boot = [{"type": "info", "severity": "info",
             "payload": {"agent": "hub-agent", "version": AGENT_VERSION},
             "occurred_at": _now_iso(),
             "dedup_key": "agent-boot-" + AGENT_VERSION}]

    acks, evenements = [], []
    # Commandes reçues pendant un envoi intermédiaire : le serveur les a
    # marquées « livrées », c'est donc à nous de les exécuter — au tour suivant.
    en_retard = []
    backoff = 5
    while True:
        try:
            # L'instantané part à CHAQUE contact : c'est lui qui remplace
            # « l'agent a appelé » par « voilà ce que je vois ». Il donne au
            # passage la version du cœur, qui peut avoir changé.
            sante = []
            version_ha = None
            try:
                evt = instantane_sante(ha, sup, store)
                sante = [evt]
                version_ha = evt["payload"].get("core")
                # La courbe fine des kits d'essai — muette ailleurs.
                mesure = evenement_temperature((evt["payload"].get("pad") or {}).get("temperature"))
                if mesure:
                    sante.append(mesure)
            except Exception as e:
                print("[hub-agent] instantané KO:", e, flush=True)

            differes = []
            # Ce que le serveur de la tablette a signalé entre deux tours : il
            # tourne dans un autre fil et ne peut pas parler au serveur lui-même.
            while _EVENEMENTS_HORS_BOUCLE:
                evenements.append(_EVENEMENTS_HORS_BOUCLE.pop(0))
            rep = sync_once(hub_url, hub_key, events=boot + evenements + sante, acks=acks)
            boot, evenements = [], []                 # le boot n'est envoyé qu'une fois
            commandes = en_retard + list(rep.get("commands", []))
            en_retard = []
            acks = traiter(commandes, ha, store, sup, version_ha, differes)

            if differes:
                # On FAIT PARTIR les accusés (et l'annonce de début) AVANT
                # d'agir : la suite peut nous tuer — mise à jour de l'agent —
                # ou couper Home Assistant. Une commande sans réponse serait
                # re-livrée sans fin ; ici, elle est déjà close.
                # ⚠️ CETTE RÉPONSE NE SE JETTE PAS. Le serveur en profite pour
                # livrer les commandes en attente : les ignorer les laissait
                # marquées « livrées » sans que personne ne les exécute, et
                # elles n'étaient reprises qu'après le délai de re-livraison.
                # On les garde pour le tour suivant.
                rep2 = sync_once(hub_url, hub_key,
                                 events=[_evt_maintenance("debut", n, cible=c) for n, _, c in differes],
                                 acks=acks)
                acks = []
                for c in (rep2 or {}).get("commands", []) or []:
                    en_retard.append(c)
                for nom, action, cible in differes:
                    try:
                        evenements.append(_evt_maintenance("fin", nom, action(), cible))
                    except Exception as e:
                        print("[hub-agent] entretien KO (%s):" % nom, e, flush=True)
                        evenements.append(
                            _evt_maintenance("echec", nom, {"error": str(e)}, cible))
                continue        # on repart tout de suite pour remonter le résultat

            # Une interface fraîchement déployée n'arrive à l'écran que si
            # quelqu'un recharge la page. On le fait ici, après les commandes
            # (donc après le déploiement), et on réessaie chaque tour tant
            # que la tablette n'a pas été jointe.
            try:
                rafraichir_pad_si_besoin()
            except Exception as e:
                print("[hub-agent] rafraîchissement du pad KO :", e, flush=True)

            backoff = 5
            # s'il restait des commandes, on renvoie tout de suite les acks
            time.sleep(0 if acks else intervalle)
        except Exception as e:
            print("[hub-agent] sync KO:", e, flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, intervalle)    # recul progressif, plafonné


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


if __name__ == "__main__":
    main()
