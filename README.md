# Osc-PRA

Application web de gestion de Plans de Reprise d'Activité (PRA) sur
[Outscale](https://outscale.com). Elle automatise la réplication de VMs
(snapshots planifiés, VM cible tenue à jour) et centralise le suivi de ces
opérations.

Nom/logo affichés dans l'application : **Outscale Net Replicator** — nom du
projet et du dépôt inchangés (`osc-pra`).

Dépôt : https://github.com/glorp-fr/osc-pra
Spécification fonctionnelle détaillée : [CLAUDE.MD](CLAUDE.MD)
Suivi des chantiers en cours : [TODO.md](TODO.md)

## Fonctionnement

Un **plan de reprise** relie un compte/VPC source à un compte/VPC cible et
liste les VMs à répliquer. Selon la fréquence configurée, un job planifié
(cron) exécute pour chaque VM sélectionnée :

### Cible dans la même région

1. **Snapshot** des disques (BSU) de la VM source, purge des anciens
   snapshots au-delà du nombre à conserver.
2. **Création de la VM cible** dans le VPC de PRA, si elle n'existe pas
   encore (même image, même type d'instance ; subnet et security groups
   résolus côté cible par correspondance de nom — voir
   *Resynchronisation des ressources réseau* ci-dessous).
3. **Restauration des disques** : les volumes de la VM cible sont
   remplacés par des volumes recréés depuis les derniers snapshots.
4. **Attachement** des disques sur les mêmes points de montage
   (`DeviceName`) que sur la VM source.

La VM cible reste **à l'arrêt** entre deux cycles (réplique froide) : elle
n'est démarrée que manuellement, lors d'un vrai basculement. Le mapping
VM source → VM cible est mémorisé en base (table `vm_targets`) pour que
les cycles suivants réutilisent la même VM cible plutôt que d'en recréer
une.

### Cible dans une autre région (cross-région)

Le mécanisme prévu passe par un export/import du snapshot via un bucket
S3 de synchronisation avant d'appliquer les mêmes étapes 2 à 4. **Non
implémenté à ce jour** — voir [TODO.md](TODO.md). Un plan configuré en
cross-région exécute bien les snapshots source, mais le job le signale
explicitement plutôt que de tenter une restauration.

### Resynchronisation des ressources réseau

Le VPC cible (« VPC de PRA ») doit être créé une première fois depuis la
page *Modifier* d'un plan. Pour que la création de VM cible fonctionne
(étape 2), le VPC cible a aussi besoin des subnets, security groups,
internet service et tables de routage correspondants côté source
(`app.target.sync_target_network`) : les objets manquants côté cible
(identifiés par leur tag `Name`) sont recréés à l'identique, avec les
règles de security group et les routes (hors routes vers un Net peering
ou une passerelle VPN, non répliquées — voir *Connu, non planifié* dans
[TODO.md](TODO.md)). Tous les tags de la ressource source sont repris sur
son équivalent cible (pas seulement `Name`), remis en phase à chaque
cycle.

Cette resynchronisation se déclenche de deux façons : automatiquement à
chaque exécution planifiée du plan si l'option **Mise à jour du VPC cible
automatique** est cochée sur le plan (cochée par défaut — page
*Modifier*), ou manuellement depuis la page **Visualiser** (bouton *Mettre
à jour depuis la source*).

## Fonctionnalités

- **Authentification locale** avec rôles (admin / opérateur / lecture
  seule).
- **Gestion des plans de reprise** : AK/SK source (et cible, en
  cross-région), sélection du VPC et des VMs par scan du compte,
  fréquence de snapshot, nombre de snapshots à conserver, mise à jour du
  VPC cible automatique (activée par défaut), activation/désactivation,
  suppression.
- **Test de validité des AK/SK** (via `ReadAccessKeys`) à la saisie, avec
  date de fin de validité affichée.
- **Suivi** (`/suivi`) et **liste des plans** (`/admin/plans`) : statut
  AK/SK (en rouge si expiration à moins de 7 jours ou clé invalide),
  date/heure et statut de la dernière exécution de chaque plan (en erreur
  si au moins une VM du dernier cycle a échoué), historique des jobs,
  alertes.
- **Visualisation d'un plan** (`/admin/plans/{id}/visualiser`) :
  ressources réseau côté cible (VPC/subnets/security groups/route
  tables) avec resynchronisation à la demande, et un tableau par VM avec
  nombre de points de sauvegarde disponibles, date du dernier snapshot,
  statut (Activé et fonctionnel / Désactivé / Défaillant), et la
  dernière date à laquelle toutes les VMs du plan ont un snapshot réussi
  (dernier point de restauration complet).
- **Planification** via cron : snapshots des plans actifs et sauvegarde
  de la configuration, à la fréquence configurée dans chaque plan /
  dans les paramètres globaux.
- **Sauvegarde de la base** vers un bucket S3 (manuelle ou planifiée,
  avec rétention configurable).
- **Sécurité** (`/admin/securite`, admins) : journal d'audit des
  connexions/déconnexions/échecs de connexion et des actions de
  modification (comptes, plans, paramètres) — utilisateur, IP, date/heure,
  détail.

Ce que l'application ne fait *pas encore* : réplication cross-région,
reprise de l'IP privée de la VM source, bascule (failover) automatisée,
reporting/alerting par email. Détail dans [TODO.md](TODO.md).

## Stack technique

- **Backend** : Python 3 / [FastAPI](https://fastapi.tiangolo.com/),
  serveur ASGI [uvicorn](https://www.uvicorn.org/).
- **Frontend** : rendu serveur Jinja2 + [htmx](https://htmx.org/) (pas de
  build JS séparé). Charte graphique reprise de l'identité OUTSCALE.
- **Base de données** : SQLite (fichier unique embarqué, `data/osc-pra.db`).
- **Intégration Outscale** : [octl](https://github.com/outscale/octl),
  invoqué en sous-processus (`octl iaas api <Action> --output json`),
  credentials passés par variables d'environnement uniquement (jamais
  écrits sur disque). Les SK stockés en base sont chiffrés
  (Fernet/`cryptography`) avec une clé locale (`data/encryption.key`).
- **Scheduling** : cron système (le crontab de l'utilisateur qui fait
  tourner le service est régénéré automatiquement à chaque changement de
  plan ou de paramètres, dans un bloc balisé).

## Installation

Distributions supportées : Debian, Ubuntu.

```bash
sudo ./setup.sh
```

Le script :

- installe les dépendances système (`python3`, `cron`, `nginx`...) et
  [octl](https://github.com/outscale/octl) ;
- crée un environnement virtuel Python et installe `requirements.txt` ;
- crée le compte admin (mot de passe demandé à l'exécution —
  `scripts/create_admin.py`) ;
- installe et démarre le service systemd (`deploy/osc-pra.service.template`) ;
- configure nginx en reverse proxy (port 80), avec activation HTTPS
  optionnelle via Let's Encrypt (FQDN requis).

## Exploitation

```bash
# Statut / logs du service
sudo systemctl status osc-pra
sudo journalctl -u osc-pra -f

# Après un déploiement de code : pas de rechargement automatique,
# il faut redémarrer le service pour charger le nouveau code
sudo systemctl restart osc-pra

# Logs des jobs planifiés (cron)
tail -f data/cron.log

# Crontab généré pour l'utilisateur du service
crontab -l
```

Le service tourne sans `--reload` (`app/main.py` via `uvicorn`) : toute
modification du code Python nécessite un redémarrage du service pour être
prise en compte (les templates Jinja2, eux, sont relus à chaque requête).

## Structure du projet

```
app/
  main.py            Point d'entrée FastAPI, montage des routers
  db.py              Schéma SQLite + migrations légères (ALTER TABLE)
  auth.py            Authentification / rôles
  audit.py           Journal d'audit (connexions, actions de modification — table `audit_log`)
  octl.py            Wrapper autour du CLI octl (toutes les actions API Outscale)
  restore.py         Orchestration de la restauration des BSU sur la VM cible
  target.py          Résolution des identifiants du compte cible et
                      resynchronisation du réseau cible (subnets, security
                      groups + règles, internet service, tables de routage)
  cron.py            Génération/synchronisation du crontab utilisateur
  jobs.py            Historique des jobs (table `jobs`) et statuts agrégés par plan
  crypto.py          Chiffrement des SK stockés en base (Fernet)
  secret.py          Clé de signature de session
  scheduling.py      Fréquences de planification (cron expressions)
  version.py         Version courante (fichier VERSION) et détection de mise à jour
  templates_env.py   Instance Jinja2Templates + globals (asset_version, app_version...)
  routers/
    auth.py          Connexion / déconnexion
    suivi.py          Page de suivi (plans, jobs, alertes AK/SK)
    admin.py          Zone admin (plans, comptes, paramètres, visualisation)
  templates/         Templates Jinja2 (dont admin/ pour la zone admin)
  static/            CSS, assets (dont le logo, static/assets/logo.png)

scripts/
  run_plan.py        Job cron : snapshot + restauration pour un plan
  run_backup.py       Job cron : sauvegarde de la base vers S3
  create_admin.py    Création du compte admin (setup initial)

deploy/
  osc-pra.service.template   Unit systemd (templaté par setup.sh)

data/                Base SQLite, clés de chiffrement, logs cron (non versionné)

VERSION               Version SemVer courante (voir CLAUDE.MD, Versioning et mises à jour)
update.sh             Met à jour une instance déployée vers un tag vX.Y.Z
```

## Sécurité

- Les secrets (AK/SK) saisis sont chiffrés en base (Fernet) et déchiffrés
  uniquement en mémoire au moment de l'appel à `octl` ; ils transitent
  par variables d'environnement du sous-processus, jamais sur disque en
  clair.
- Les sessions utilisateur sont signées (`itsdangerous`, clé locale
  `data/session.key`).
- Le fichier `creds.py` à la racine (identifiants locaux, hors du dépôt)
  est ignoré par git (`.gitignore`).
