#!/usr/bin/env python3
"""."""


import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request


AGENT_VERSION = "0.3.0"


CHEMINS_AUTORISES = ("automations_brightstay/", "blueprints/", "packages/brightstay")
DOMAINES_RECHARGEABLES = {"automation", "script", "template", "input_boolean",
                          "input_number", "input_select", "scene", "group"}


def version_au_moins(version, minimum):
    if not minimum:
        return True
    if not version:
        return False
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
        return False
    for i in range(max(len(a), len(b))):
        x = a[i] if i < len(a) else 0
        y = b[i] if i < len(b) else 0
        if x > y:
            return True
        if x < y:
            return False
    return True


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
        """."""
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
        """."""


        return self._req("GET", "/api/config") or {}

    def repond(self):
        """."""


        try:
            r = self._req("GET", "/api/")
            return bool(r), None
        except Exception as e:
            return False, str(e)


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

            return rep.get("data", rep)


    def info(self):
        """."""


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


    def recharger_boutique(self):
        """."""


        self._req("POST", "/store/reload", {}, timeout=300)
        return {"boutique": "relue"}

    def version_boutique(self, slug="self"):
        """."""
        infos = self._req("GET", "/addons/%s/info" % slug) or {}
        return infos.get("version_latest")

    def maj_addon(self, slug="self", version=None):
        """."""


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


PAD_RACINE = os.environ.get("BS_PAD_RACINE", "/data/pad")


PAD_WEB_PORT = int(os.environ.get("BS_PAD_WEB_PORT", "8099"))
PAD_MAX_OCTETS = 64 * 1024 * 1024
PAD_GARDE = 3


COUCHES = ("habillage", "illustrations", "page", "complet")


def _pad_chemins(couche="complet"):
    """."""


    if couche == "complet":
        return (os.path.join(PAD_RACINE, "versions"), os.path.join(PAD_RACINE, "courant"))
    if couche not in COUCHES:
        raise ValueError("couche inconnue : " + str(couche))
    base = os.path.join(PAD_RACINE, "couches", couche)
    return (os.path.join(base, "versions"), os.path.join(base, "courant"))


def _chemin_dans_couches(chemin_url):
    """."""


    chemin = urllib.parse.unquote(chemin_url.split("?", 1)[0].split("#", 1)[0])
    morceaux = []
    for m in chemin.split("/"):
        if not m or m == ".":
            continue


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


    if corps.get("ip") and not _PAD_CONNU.get("ip"):
        _PAD_CONNU["ip"] = corps["ip"]
    return corps


def derniere_annonce():
    try:
        with open(_annonce_chemin(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def ecrire_config_pad(config, version=None):
    """."""
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
    """."""


    try:
        with open(_config_chemin(), encoding="utf-8") as f:
            return json.load(f).get("_version")
    except (OSError, ValueError):
        return None


def version_pad_servie(couche="complet"):
    """."""

    _, courant = _pad_chemins(couche)
    try:
        return os.path.basename(os.path.realpath(courant)) if os.path.islink(courant) else None
    except OSError:
        return None


def couches_servies():
    """."""


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
    """."""


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
    """."""


    try:
        os.makedirs(PAD_RACINE, exist_ok=True)
        with open(_fichier_pad_a_rafraichir(), "w", encoding="utf-8") as f:
            f.write(str(version or ""))
    except OSError as e:
        print("[hub-agent] marque de rafraîchissement non posée :", e, flush=True)


def rafraichir_pad_si_besoin(mot_de_passe=None):
    """."""


    if not os.path.exists(_fichier_pad_a_rafraichir()):
        return None


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
    """."""

    racine = os.path.abspath(cible)
    for membre in zf.namelist():
        if membre.startswith("/") or ".." in membre.split("/"):
            raise ValueError("chemin refusé dans l'archive : " + membre)
        dest = os.path.abspath(os.path.join(racine, membre))
        if os.path.commonpath([dest, racine]) != racine:
            raise ValueError("échappement de l'archive : " + membre)
    zf.extractall(racine)


def deployer_pad(version, url, empreinte, couche="complet"):
    """."""


    import shutil
    import zipfile
    if not (version and url and empreinte):
        raise ValueError("version, url et empreinte sont tous obligatoires")

    versions, courant = _pad_chemins(couche)
    os.makedirs(versions, exist_ok=True)
    cible = os.path.join(versions, str(version))

    if os.path.isdir(cible):

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
    """."""
    tmp = courant + ".tmp"
    try:
        os.unlink(tmp)
    except OSError:
        pass
    os.symlink(cible, tmp)
    os.replace(tmp, courant)


def _purger_versions(versions, courant):
    """."""

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
    """."""


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


ACCES_RESERVES = ("ha_url", "ha_token")


MARQUE_HUB = "{hub}"


def adresse_vue_depuis(ip_cible):
    """."""


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
    """."""


    if not isinstance(valeur, str) or not adresse or adresse not in valeur:
        return valeur
    return valeur.replace(adresse, MARQUE_HUB)


PLAGES_INTERNES = ("172.30.32.", "172.30.33.", "172.17.")


def _adresse_dans_la_maison(a):
    """."""


    if not a:
        return False
    a = a.strip().lower()
    if a in ("127.0.0.1", "::1", "0.0.0.0", "localhost"):
        return False
    if a.startswith("127.") or a.startswith("169.254."):
        return False
    return not any(a.startswith(p) for p in PLAGES_INTERNES)


def _adresse_annoncee(en_tete_host):
    """."""


    if not en_tete_host:
        return None
    h = en_tete_host.strip()
    if h.startswith("["):
        fin = h.find("]")
        h = h[1:fin] if fin > 0 else ""
    else:
        h = h.rsplit(":", 1)[0] if h.count(":") == 1 else h
    import ipaddress as _ip
    try:
        adr = _ip.ip_address(h)
    except ValueError:
        return None
    if not adr.is_private or adr.is_loopback or adr.is_link_local:
        return None
    return h if _adresse_dans_la_maison(h) else None


def _adresse_joignable(url_ha, adresse_locale, en_tete_host=None):
    """."""


    import urllib.parse as _parse
    u = _parse.urlsplit(url_ha or "")
    if not u.hostname:
        return None
    locaux = ("localhost", "127.0.0.1", "::1", "0.0.0.0", "supervisor")
    if u.hostname.lower() not in locaux:
        return url_ha.rstrip("/")
    adresse = adresse_locale if _adresse_dans_la_maison(adresse_locale) else None
    if adresse is None:
        adresse = _adresse_annoncee(en_tete_host)
    if adresse is None:
        return None
    hote = "[%s]" % adresse if ":" in adresse else adresse
    port = u.port or (443 if u.scheme == "https" else 8123)
    return "%s://%s:%d" % (u.scheme or "http", hote, port)


def _jeton_pour_la_tablette(jeton):
    """."""


    if not jeton or not isinstance(jeton, str):
        return None
    morceaux = jeton.strip().split(".")
    if len(morceaux) != 3 or not all(len(m) >= 8 for m in morceaux):
        return None
    return jeton.strip()


def config_pour_la_tablette(url_ha, jeton_ha, adresse_locale, en_tete_host=None):
    """."""
    try:
        with open(_config_chemin(), encoding="utf-8") as f:
            conf = json.load(f)
        if not isinstance(conf, dict):
            conf = {}
    except Exception:
        conf = {}


    for cle in ACCES_RESERVES:
        conf.pop(cle, None)


    adresse = _adresse_joignable(url_ha, adresse_locale, en_tete_host)
    jeton = _jeton_pour_la_tablette(jeton_ha)
    if adresse and jeton:
        conf["ha_url"] = adresse
        conf["ha_token"] = jeton
    return conf


def demarrer_serveur_pad(ha_url=None, ha_token=None):
    """."""


    import functools
    import threading
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    versions, courant = _pad_chemins()
    os.makedirs(versions, exist_ok=True)

    class Poli(SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            """."""


            if self.path.split("?")[0] != "/annonce":
                self.send_response(404); self.end_headers(); return
            try:
                n = int(self.headers.get("content-length") or 0)
                corps = json.loads(self.rfile.read(min(n, 8192)) or b"{}")
                if not isinstance(corps, dict):
                    corps = {}
            except Exception:
                corps = {}

            corps["ip"] = self.client_address[0]
            corps["vu_a"] = _now_iso()
            try:
                enregistrer_annonce(corps)
            except Exception:
                pass


            self.send_response(204); self.end_headers()

        def do_GET(self):


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


                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(corps)))
                self.end_headers()
                self.wfile.write(corps)
                return
            SimpleHTTPRequestHandler.do_GET(self)

        def translate_path(self, path):
            """."""


            resolu = _chemin_dans_couches(path)


            return resolu if resolu else os.path.join(PAD_RACINE, ".refuse")

        def end_headers(self):


            self.send_header("Cache-Control", "no-cache")
            SimpleHTTPRequestHandler.end_headers(self)


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


PAD_PORT = 2323


REGLAGES_SUIVIS = (
    "startURL", "kioskMode", "launchOnBoot", "singleAppMode", "singleAppIntent",
    "useWideViewport", "screenBrightness", "remoteAdmin", "keepScreenOn",
    "showNavigationBar", "showActionBar", "advancedKioskProtection",
    "desktopMode", "restartOnCrash", "timeToScreensaverV2", "sleepSchedule",
    "preventSleepWhileScreenOff",

    "errorURL", "loadContentZipFileUrl", "reloadPageFailure",
    "errorUrlOnDisconnection",


    "reloadOnWifiOn", "resetWifiOnDisconnection",
)


_REGLAGES_APPRIS = set()


def _fichier_reglages_appris():
    return os.path.join(PAD_RACINE, "reglages-appris.json")


def _charger_reglages_appris():
    """."""
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


PAD_COMMANDES_AUTORISEES = {
    "loadUrl", "screenOn", "screenOff", "toForeground",
    "clearCache", "setStringSetting", "setBooleanSetting", "deviceInfo", "listSettings",


}


_PAD_CONNU = {"ip": os.environ.get("BS_PAD_IP")}


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
        os.chmod(chemin, 0o600)
    except OSError:
        pass
    if ip:
        _PAD_CONNU["ip"] = ip


    return {"acces": "enregistré"}


def _mdp_pad():
    """."""

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
    """."""


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
        return [force]


    try:
        with open("/proc/net/route", encoding="utf-8") as f:
            lignes = f.read().strip().split("\n")[1:]
        for ligne in lignes:
            c = ligne.split()
            if len(c) < 8:
                continue
            dest, masque = int(c[1], 16), int(c[7], 16)
            if dest == 0 or masque == 0:
                continue

            octets = [(dest >> (8 * i)) & 0xFF for i in range(4)]
            bits = bin(masque).count("1")
            if bits >= 24:
                ajouter("%d.%d.%d.%d" % tuple(octets))
    except OSError:
        pass

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
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

    return bases[:4]


def trouver_pads(mot_de_passe, timeout=0.35, limite=None):
    """."""


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

                    if Pad(ip, mot_de_passe, timeout=4).info().get("packageName"):
                        trouves.append(ip)
                        if limite and len(trouves) >= limite:
                            return trouves
                except Exception:
                    pass
    return trouves


def trouver_pad(mot_de_passe, timeout=0.35):
    """."""
    trouves = trouver_pads(mot_de_passe, timeout, limite=1)
    return trouves[0] if trouves else None


def _identite_pad(infos):
    """."""


    if not isinstance(infos, dict):
        return None
    for cle in ("deviceID", "deviceId", "serial", "Mac", "mac"):
        v = infos.get(cle)
        if v:
            return str(v)
    return None


def _pad(mot_de_passe=None, rebalayer=True):
    """."""


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
    """."""

    mdp = mot_de_passe or _mdp_pad()
    if not mdp:
        return None


    a = derniere_annonce() or {}
    socle = {}
    if a.get("vu_a"):
        socle = {"annonce_vu_a": a["vu_a"], "annonce_ip": a.get("ip"),
                 "annonce_version": a.get("version"), "annonce_page": a.get("page")}

    p = _pad(mdp)
    if p is None:


        socle["joignable"] = False
        if socle.get("annonce_vu_a"):
            socle["isolee"] = True
        return socle
    try:
        info = p.info()
    except Exception as e:
        return {"joignable": False, "erreur": str(e)[:120]}


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


        "verrouille": bool(info.get("keyguardLocked")),
        "ecran_verrouille": bool(info.get("screenLocked")),
        "veille_forcee": bool(info.get("isInForcedSleep")),
        "economiseur": bool(info.get("isInScreensaver")),
        "version_fully": info.get("appVersionName"),
    })
    try:
        tous = p.reglages() or {}
        etat["reglages"] = {k: _remarquer_hub(tous[k], mienne)
                            for k in reglages_a_rapporter() if k in tous}
        etat["hub_ip"] = mienne
    except Exception as e:


        etat["reglages_erreur"] = str(e)[:120]
        print("[hub-agent] réglages du pad NON rapportés (le serveur va "
              "les redemander sans fin) :", e, flush=True)
    return etat


class Store:
    def __init__(self, config_dir):
        self.root = os.path.abspath(config_dir)

    def _resoudre(self, rel):

        if not rel or rel.startswith("/") or ".." in rel.split("/"):
            raise ValueError("chemin refusé : " + rel)
        if not any(rel == p or rel.startswith(p) for p in CHEMINS_AUTORISES):
            raise ValueError("hors du périmètre Brightstay : " + rel)
        cible = os.path.abspath(os.path.join(self.root, rel))

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
    """."""
    for rel, avant in snapshot.items():
        if avant is None:
            store.delete(rel)
        else:
            store.put(rel, avant)


def _differer(differes, nom, action, resume):
    """."""


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


        files = p.get("files", [])
        reload_domains = p.get("reload", [])
        for d in reload_domains:
            if d not in DOMAINES_RECHARGEABLES:
                return "failed", {"error": "domaine non rechargeable : " + str(d)}


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


        chk = ha.check_config()
        if chk.get("result") != "valid":
            return "failed", {"refused": "check_config invalide", "errors": chk.get("errors")}
        ha.reload(domain)
        return "acked", {"reloaded": domain}

    if t == "hub.service":

        ha.call_service(p["domain"], p["service"], p.get("data"))
        return "acked", {"called": p["domain"] + "." + p["service"]}

    if t == "hub.inventaire":


        pret, raison = ha_pret(ha)
        if not pret:
            return "failed", {"error": raison}
        return "acked", inventaire(ha)


    if t.startswith("hub.addon.") or t.startswith("hub.core.") \
       or t.startswith("hub.backup.") or t.startswith("hub.host."):
        if sup is None:
            return "failed", {"error": "Superviseur indisponible — l'add-on doit tourner "
                                       "sur un hub Home Assistant avec hassio_api"}

    if t == "hub.addon.update":


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


        cmdp = p.get("cmd")
        if cmdp not in PAD_COMMANDES_AUTORISEES:
            return "failed", {"error": "commande de pad non autorisée : " + str(cmdp)}
        pad = _pad(p.get("mot_de_passe"))
        if pad is None:
            return "failed", {"error": "pad introuvable sur le réseau local"}
        params = {k: v for k, v in (p.get("params") or {}).items()}

        for k, v in list(params.items()):
            if isinstance(v, bool):
                params[k] = "true" if v else "false"


        if cmdp in ("setStringSetting", "setBooleanSetting"):
            _apprendre_reglage(params.get("key"))

            if "value" in params:
                params["value"] = _substituer_hub(
                    params["value"], adresse_vue_depuis(pad.ip))
        return "acked", {"pad": pad.ip, "cmd": cmdp, "reponse": pad.commande(cmdp, **params)}

    if t == "hub.pad.deploy":


        resultat = deployer_pad(p.get("version"), p.get("url"), p.get("sha256"),
                                p.get("couche") or "complet")


        marquer_pad_a_rafraichir(p.get("version"))
        return "acked", resultat

    if t == "hub.pad.identite":


        return "acked", enregistrer_acces_pad(p.get("mot_de_passe"), p.get("ip"))

    if t == "hub.pad.config":


        return "acked", ecrire_config_pad(p.get("config") or {}, p.get("version"))

    if t == "hub.pad.rollback":


        return "acked", revenir_pad(p.get("version"), p.get("couche") or "complet")

    if t == "hub.versions":

        core = sup.info_core() or {}
        addons = (sup.info_addons() or {}).get("addons", [])
        return "acked", {
            "agent": AGENT_VERSION,
            "core": core.get("version"),
            "core_disponible": core.get("version_latest"),
            "addons": {a.get("slug"): a.get("version") for a in addons},
        }

    return "failed", {"error": "type de commande inconnu : " + str(t)}


def sync_once(hub_url, hub_key, events=None, acks=None, timeout=20):
    body = json.dumps({"events": events or [], "acks": acks or []}).encode()
    req = urllib.request.Request(hub_url, data=body, method="POST")
    req.add_header("x-hub-key", hub_key)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def traiter(commands, ha, store, sup=None, version_ha=None, differes=None):
    """."""


    acks = []
    for cmd in commands:
        try:
            status, result = dispatch(cmd, ha, store, sup, version_ha, differes)
        except Exception as e:
            status, result = "failed", {"error": str(e)}
        acks.append({"command_id": cmd["id"], "status": status, "result": result})
    return acks


CLASSES_SECURITE = {"smoke", "gas", "carbon_monoxide", "moisture"}
MAX_LISTE = 15


DOMAINES_INVENTAIRE = {"light", "cover", "climate", "media_player", "lock", "fan", "switch"}


CLASSES_BINAIRES_UTILES = CLASSES_SECURITE | {"window", "opening", "garage_door", "door", "power"}
MAX_INVENTAIRE = 400


def ha_pret(ha):
    """."""


    try:
        etat = (ha.config() or {}).get("state")
    except Exception:
        return True, None
    if etat and etat != "RUNNING":
        return False, "Home Assistant démarre encore (%s)" % etat
    return True, None


def inventaire(ha):
    """."""
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


        etat = e.get("state")
        a = {
            "id": eid,
            "domaine": domaine,


            "nom": attrs.get("friendly_name") or eid,
            "disponible": etat not in (None, "unavailable", "unknown"),
        }
        if classe:
            a["classe"] = str(classe)

        if domaine == "climate":
            modes = attrs.get("hvac_modes")
            if isinstance(modes, list):

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
    """."""


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


            infos = _ssl._ssl._test_decode_cert(chemin)
        finally:
            os.unlink(chemin)
        fin = _ssl.cert_time_to_seconds(infos["notAfter"])
        return int((fin - time.time()) // 86400)
    except Exception:
        return None


MAX_RECETTES = 40


def _empreintes_recettes(store):
    """."""


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
    """."""


    snap = {"agent": AGENT_VERSION}


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

        snap["pad_acces"] = bool(_mdp_pad())
        pad = etat_pad()
        if pad is not None:
            snap["pad"] = pad
    except Exception as e:
        snap["pad_erreur"] = str(e)[:120]

    try:
        snap["pad_version_servie"] = version_pad_servie()


        snap["pad_couches_servies"] = couches_servies()


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
    """."""


    return {
        "type": "maintenance",
        "severity": "warning" if phase == "echec" else "info",
        "payload": {"phase": phase, "operation": quoi, "detail": detail or {},
                    "agent_version": AGENT_VERSION},
        "occurred_at": _now_iso(),
        "dedup_key": "maint-%s-%s-%s" % (phase, quoi, _now_iso()),
    }


def main():
    hub_url = os.environ["BS_HUB_SYNC_URL"]
    hub_key = os.environ["BS_HUB_KEY"]
    ha_url = os.environ.get("HA_URL", "http://supervisor/core")
    ha_token = os.environ.get("HA_TOKEN") or os.environ.get("SUPERVISOR_TOKEN", "")


    ha_token_pad = os.environ.get("BS_PAD_HA_TOKEN", "")
    config_dir = os.environ.get("HA_CONFIG_DIR", "/homeassistant")
    intervalle = int(os.environ.get("BS_SYNC_INTERVAL", "300"))

    ha = HA(ha_url, ha_token)
    store = Store(config_dir)
    _charger_reglages_appris()


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


    boot = [{"type": "info", "severity": "info",
             "payload": {"agent": "hub-agent", "version": AGENT_VERSION},
             "occurred_at": _now_iso(),
             "dedup_key": "agent-boot-" + AGENT_VERSION}]

    acks, evenements = [], []


    en_retard = []
    backoff = 5
    while True:
        try:


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
            boot, evenements = [], []
            commandes = en_retard + list(rep.get("commands", []))
            en_retard = []
            acks = traiter(commandes, ha, store, sup, version_ha, differes)

            if differes:


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
                continue


            try:
                rafraichir_pad_si_besoin()
            except Exception as e:
                print("[hub-agent] rafraîchissement du pad KO :", e, flush=True)

            backoff = 5

            time.sleep(0 if acks else intervalle)
        except Exception as e:
            print("[hub-agent] sync KO:", e, flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, intervalle)


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


if __name__ == "__main__":
    main()
