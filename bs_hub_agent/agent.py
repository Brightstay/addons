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
import json
import os
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
DOMAINES_RECHARGEABLES = {"automation", "script", "template", "input_boolean",
                          "input_number", "input_select", "scene", "group"}


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
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            txt = r.read().decode()
            return json.loads(txt) if txt else {}

    def check_config(self):
        """Valide la config SANS l'appliquer. {'result':'valid'|'invalid','errors':...}"""
        return self._req("POST", "/api/config/core/check_config", {})

    def reload(self, domain):
        return self._req("POST", "/api/services/%s/reload" % domain, {})

    def call_service(self, domain, service, data=None):
        return self._req("POST", "/api/services/%s/%s" % (domain, service), data or {})

    def state(self, entity_id):
        try:
            return self._req("GET", "/api/states/" + entity_id)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def states(self):
        return self._req("GET", "/api/states")

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

    def maj_addon(self, slug="self", version=None):
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
        self._req("POST", "/addons/%s/update" % slug, {}, timeout=900)
        return {"addon": slug, "version": version or "celle de la boutique"}

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

    def redemarrer_hote(self):
        self._req("POST", "/host/reboot", {}, timeout=60)
        return {"hote": "redémarrage demandé"}


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
COUCHES = ("habillage", "illustrations", "page", "complet")


def _pad_chemins(couche="complet"):
    """Où vivent les versions d'une couche, et quel lien désigne celle servie.

    `complet` garde l'emplacement historique : un boîtier déjà installé n'a
    rien à déménager."""
    if couche == "complet":
        return (os.path.join(PAD_RACINE, "versions"), os.path.join(PAD_RACINE, "courant"))
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
ACCES_RESERVES = ("ha_url", "ha_token")

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
    adresse = _adresse_joignable(url_ha, adresse_locale, en_tete_host)
    jeton = _jeton_pour_la_tablette(jeton_ha)
    if adresse and jeton:
        conf["ha_url"] = adresse
        conf["ha_token"] = jeton
    return conf


def demarrer_serveur_pad(ha_url=None, ha_token=None):
    """Sert le dossier courant, en HTTPS si le hub a son certificat.

    Sans certificat, on sert quand même en clair — mais on le DIT : sans
    contexte sécurisé, pas de service worker, donc pas de démarrage hors
    ligne. C'est utilisable en développement, jamais en logement."""
    import functools
    import threading
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

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
            if self.path.split("?")[0] != "/annonce":
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

        def do_GET(self):
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

# Simple CACHE (pas un état : perdu au redémarrage, on re-balaie et c'est tout).
_PAD_CONNU = {"ip": os.environ.get("BS_PAD_IP")}


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


def enregistrer_acces_pad(mot_de_passe, ip=None):
    if not mot_de_passe:
        raise ValueError("mot de passe vide")
    os.makedirs(PAD_RACINE, exist_ok=True)
    chemin = _acces_chemin()
    tmp = chemin + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"mot_de_passe": mot_de_passe, "ip": ip}, f)
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
    for base in _reseaux_locaux():
        adresses = ["%s.%d" % (base, n) for n in range(1, 255)]
        with ThreadPoolExecutor(max_workers=48) as ex:
            for ip in ex.map(ouvert, adresses):
                if not ip:
                    continue
                try:
                    # port ouvert ≠ notre pad : on vérifie que c'est bien Fully
                    if Pad(ip, mot_de_passe, timeout=4).info().get("packageName"):
                        trouves.append(ip)
                        if limite and len(trouves) >= limite:
                            return trouves
                except Exception:
                    pass
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
            if identite and not attendue:
                _PAD_CONNU["identite"] = identite
            _PAD_CONNU["ip"] = ip
            return Pad(ip, mdp)
        except Exception:
            pass
    if not rebalayer:
        return None
    ip = trouver_pad(mdp)
    _PAD_CONNU["ip"] = ip
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
def _differer(differes, nom, action, resume):
    """Acquitte MAINTENANT, agit APRÈS que l'accusé de réception soit parti.

    Indispensable pour tout ce qui coupe le tapis sous nos pieds : mettre à
    jour l'agent tue son propre processus, mettre à jour le cœur redémarre
    Home Assistant. Si on agissait d'abord, la commande resterait sans
    réponse et le serveur la re-livrerait en boucle.
    `differes = None` ⇒ mode synchrone, pour les tests unitaires."""
    if differes is None:
        return "acked", action()
    differes.append((nom, action))
    return "acked", {"lance": resume}


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
        # ex. serrure : {domain:'lock', service:'unlock', data:{entity_id:...}}
        ha.call_service(p["domain"], p["service"], p.get("data"))
        return "acked", {"called": p["domain"] + "." + p["service"]}

    if t == "hub.inventaire":
        # « Qu'est-ce qu'il y a dans ce logement ? » — la réponse part dans
        # l'accusé, et hub-sync la recopie dans la table `inventaire`.
        pret, raison = ha_pret(ha)
        if not pret:
            return "failed", {"error": raison}
        return "acked", inventaire(ha)

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
                         lambda: sup.maj_addon(slug, version),
                         "mise à jour de l'add-on %s vers %s" % (slug, version or "la version installée"))

    if t == "hub.core.update":
        version = p.get("version")
        if not version:
            return "failed", {"error": "version cible obligatoire — on ne met jamais à jour « au dernier »"}
        return _differer(differes, "core.update",
                         lambda: sup.maj_core(version),
                         "mise à jour de Home Assistant vers " + str(version))

    if t == "hub.core.restart":
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
        resultat = deployer_pad(p.get("version"), p.get("url"), p.get("sha256"),
                                p.get("couche") or "complet")
        # Déployer ne suffit pas : la tablette affiche toujours l'ancienne
        # page tant que personne ne la recharge (constaté au Raspberry).
        marquer_pad_a_rafraichir(p.get("version"))
        return "acked", resultat

    if t == "hub.pad.identite":
        # Le serveur confie au hub de quoi parler à SA tablette. Envoyé une
        # fois, gardé ensuite — y compris après un remplacement de hub.
        return "acked", enregistrer_acces_pad(p.get("mot_de_passe"), p.get("ip"))

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

    return "failed", {"error": "type de commande inconnu : " + str(t)}


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


def inventaire(ha):
    """Ce que le hub voit, compacté pour l'écran de configuration."""
    etats = ha.states() or []
    appareils = []

    for e in etats:
        eid = e.get("entity_id") or ""
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
        indispo, secu, piles = [], [], []
        for e in etats:
            attrs = e.get("attributes") or {}
            eid = e.get("entity_id", "")
            valeur = e.get("state")
            classe = attrs.get("device_class")
            if valeur in ("unavailable", "unknown"):
                indispo.append(eid)
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

    try:
        # on dit si le hub PEUT parler à la tablette — jamais avec quoi
        snap["pad_acces"] = bool(_mdp_pad())
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


def _evt_maintenance(phase, quoi, detail=None):
    """Une opération d'entretien raconte son histoire en deux temps : elle
    annonce qu'elle commence (avant de couper la parole), puis dit comment
    elle a fini. Sans le « début », un hub qui redémarre pour une mise à jour
    légitime ressemblerait à un hub tombé en panne."""
    return {
        "type": "maintenance",
        "severity": "warning" if phase == "echec" else "info",
        "payload": {"phase": phase, "operation": quoi, "detail": detail or {},
                    "agent_version": AGENT_VERSION},
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
    store = Store(config_dir)
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
    try:
        demarrer_serveur_pad(ha_url, ha_token_pad)
    except Exception as e:
        print("[hub-agent] serveur de page KO :", e, flush=True)

    jeton_sup = os.environ.get("SUPERVISOR_TOKEN")
    sup = Supervisor(jeton_sup, os.environ.get("BS_SUPERVISOR_URL", "http://supervisor")) \
        if jeton_sup else None
    if sup is None:
        print("[hub-agent] pas de Superviseur : entretien du hub indisponible", flush=True)

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
            except Exception as e:
                print("[hub-agent] instantané KO:", e, flush=True)

            differes = []
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
                                 events=[_evt_maintenance("debut", n) for n, _ in differes],
                                 acks=acks)
                acks = []
                for c in (rep2 or {}).get("commands", []) or []:
                    en_retard.append(c)
                for nom, action in differes:
                    try:
                        evenements.append(_evt_maintenance("fin", nom, action()))
                    except Exception as e:
                        print("[hub-agent] entretien KO (%s):" % nom, e, flush=True)
                        evenements.append(_evt_maintenance("echec", nom, {"error": str(e)}))
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
