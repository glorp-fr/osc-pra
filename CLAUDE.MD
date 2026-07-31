# Osc-PRA

Projet d'application permettant de créer et gérer des Plans de Reprise d'Activité (PRA) sur Outscale.

Nom et logo affichés dans l'application (header, page de connexion, titre) :
**Outscale Net Replicator** (`app/static/assets/logo.png`) — le nom du
projet/dépôt reste `osc-pra`.

Dépôt du code : https://github.com/glorp-fr/osc-pra

## Fonctionnement attendu

- GUI avec authentification locale
  - *Authentification : version minimale pour l'instant, sera améliorée par la suite*
- Zone de suivi avec les jobs affichés
- Zone admin (réservée aux admins) où l'on peut :
  - Créer un compte
  - Créer un plan de reprise
  - Supprimer un plan de reprise
  - Désactiver un plan de reprise
  - Scanner les VMs existantes
  - Scanner les subnets existants
  - Scanner les security groups
  - Scanner les route tables
  - Mettre en place des scripts planifiés pour traiter les actions définies dans l'application
  - Alerting / reporting par email
- Chaque AK/SK saisi doit être testé : un statut valide/non valide est affiché à côté du champ de saisie, avec la date de fin de validité de l'AK

## Mécanisme de réplication

### Même région

1. Snapshot des disques de la VM source
2. Création d'une VM avec la même configuration dans le VPC de PRA (tous les
   tags de la VM source repris sur la VM cible, pas seulement `Name`, remis
   en phase à chaque cycle)
3. Restauration des disques depuis les snapshots
4. Attachement des disques dans le même ordre que sur la VM source

Avant chaque cycle, le VPC de PRA peut être resynchronisé automatiquement
depuis le VPC source (objets manquants recréés à l'identique : subnets,
security groups et leurs règles, internet service, tables de routage — tags
source repris) si l'option *Mise à jour du VPC cible automatique* du plan
est activée (par défaut) ; sinon la resynchronisation reste disponible
manuellement depuis la page Visualiser du plan.

### Cross région (autre AZ)

1. Snapshot des disques de la VM source
2. Passage obligatoire par S3 : export du snapshot vers le bucket S3 de synchro
3. Import du snapshot dans la région cible
4. Puis même fonctionnement que le cas "même région" (création VM, restauration des disques, attachement dans l'ordre)

### Suivi des jobs

Chaque VM d'un plan est traitée dans un job séparé, pour permettre un suivi individuel (statut, erreurs, reprise).

### Planification

Les commandes OCTL sont schedulées directement via cron.

## Bascule PRA

Trois façons de « faire vivre » le VPC de PRA au-delà de la réplique
froide maintenue par la sync (voir *Mécanisme de réplication*), depuis la
page Visualiser d'un plan (réservées aux admins pour Activation/Test) :

- **Activation PRA** : bascule réelle et irréversible. Démarre les VM
  cible déjà répliquées dans l'ordre configuré sur le plan (« Ordre de
  démarrage à la bascule », par VM, page Modifier), rattache l'EIP de
  production de chaque VM source à sa VM cible (`LinkPublicIp` avec
  ré-attachement direct, pas besoin de la détacher côté source au
  préalable), et reconstruit la NAT Gateway côté cible avec l'ancienne IP
  publique — ce qui nécessite de supprimer la NAT Gateway source pour
  libérer son EIP ; si la recréation échoue, une EIP neuve est allouée à
  la place et affichée bien en évidence sur la page du plan. Le plan est
  désactivé automatiquement à la fin (source présumée hors service).
- **Test de PRA** : même mécanique de démarrage, mais avec des EIP et une
  NAT Gateway neuves, jamais de modification côté source. Le plan passe en
  statut « Test PRA en cours » (badge dédié, sync planifiée suspendue) et
  se termine par une action explicite (« Terminer le test ») qui démonte
  les EIP/NAT de test, stoppe les VM cible et reprend la sync normale.
- **Sandbox** : clone un VPC entier, indépendant du VPC de PRA persistant
  du plan, et le construit comme un Test de PRA (VM restaurées depuis les
  derniers snapshots disponibles, démarrées, EIP/NAT neuves) — pour
  expérimenter sans jamais toucher au VPC de PRA officiel tenu à jour par
  la sync. Plusieurs sandbox peuvent coexister, y compris sur un même
  plan ; gérés depuis le menu **Sandbox** (liste tous plans confondus,
  actions démarrer/arrêter — VPC/EIP/NAT conservés à l'arrêt — et
  supprimer avec confirmation).

**Statut expérimental des appels EIP/NAT Gateway et Net Peering** :
`CreatePublicIp`, `LinkPublicIp`, `UnlinkPublicIp`, `DeletePublicIp`,
`CreateNatService`, `DeleteNatService`, `CreateNetPeering`,
`AcceptNetPeering`, `DeleteNetPeering` (`app/octl.py`) sont neufs et n'ont
jamais été exécutés contre un compte Outscale réel par ce code — syntaxe
confirmée via `octl iaas api <Action> --help` uniquement. À valider via un
Test de PRA (et, pour le peering, un plan à plusieurs VPC peerés côté
source) avant toute Activation PRA réelle sur un plan de production (voir
TODO.md, section *Bascule PRA et Sandbox*).

## Stack technique

- Backend : Python (FastAPI)
- Frontend : rendu serveur Jinja2 + htmx (pas de build JS séparé)
  - Charte graphique : identité OUTSCALE reprise depuis `/var/www/html/tarifs-osc` (`CHARTE_GRAPHIQUE.md` + `style.css`) — tokens de couleur (Dark Blue `#19233E`, Indigo `#5165F5`, DS Blue `#009CDE`, Yellow `#FDC605`), typographies Montserrat/Open Sans/DM Mono, composants (`.card`, `.btn`, `.bdg`, banners, sidebar)
- Base de données : SQLite (fichier unique embarqué, sans serveur à gérer)
  - *Remplace le stockage JSON brut initialement prévu : les jobs sont traités en parallèle (un job par VM), donc des écritures concurrentes sur un fichier JSON risquent la corruption. SQLite apporte les transactions ACID et le verrouillage nécessaires, tout en restant simple à déployer/sauvegarder (fichier unique)*
- Utilisation d'OCTL (https://github.com/outscale/octl)
  - Invocation : `octl iaas api <Action> --output json`, credentials passés par variables d'environnement (OSC_ACCESS_KEY/OSC_SECRET_KEY/OSC_REGION) par appel, jamais écrits sur disque
  - *Syntaxe vérifiée via la doc officielle du dépôt, mais pas encore testée contre un octl réel ni un compte Outscale valide — à valider en conditions réelles*
- Scheduling via cron (appel direct des commandes OCTL)

## Application

### Zone admin

#### 0 - Paramètres globaux

- Messagerie
  - Email source
  - Email cible
  - Serveur SMTP
- Stockage de la configuration
  - Local
  - S3 : endpoint, bucket, AK/SK
- Base de données
  - Statut : santé de la connexion, taille du fichier, date de la dernière sauvegarde
  - Sauvegarde vers bucket S3
    - Réutilisation du bucket S3 de stockage de la configuration si déjà défini, sinon bucket/endpoint/AK-SK dédiés
    - Sauvegarde manuelle (bouton) et planifiée (fréquence configurable, exécutée via cron comme les commandes OCTL)
    - Nombre de sauvegardes à conserver

#### 1 - Gestion des comptes

- Création de compte

#### 2 - Sécurité

- Journal d'audit : connexions, déconnexions et échecs de connexion des
  utilisateurs, ainsi que les actions de modification effectuées durant
  leur session (comptes, plans, paramètres) — date/heure, utilisateur,
  IP, détail

### Zone Opérateur

#### 1 - Création de Plan de reprise

- Nom du plan
- Définition AK/SK
- Cible de réplication (unique pour tout le plan — un Net Peering ne
  fonctionnant qu'au sein d'une même région, tous les VPC du plan partagent
  le même compte/région cible)
  - Même région
  - Autre région (liste de choix des régions Outscale)
    - AK/SK
    - Région
    - Bucket S3 de synchro
    - AK/SK S3
    - Endpoint S3 défini selon la région choisie
    - Nombre de snapshots à conserver côté cible (à demander à l'utilisateur)
- Mise à jour du VPC cible automatique (case à cocher, activée par défaut) :
  reconstruit les objets manquants sur chaque VPC cible à chaque
  synchronisation planifiée (voir *Mécanisme de réplication* ci-dessus)
- **VPC du plan** (un ou plusieurs — voir `app/plan_vpcs.py` et la table
  `plan_vpcs`, un plan gérait un seul VPC avant la version multi-VPC ;
  migration additive automatique au démarrage depuis les anciennes colonnes
  `plans.source_vpc_id`/`target_vpc_id`/`target_subregion`/`selected_vms`/
  `vm_image_overrides`, conservées mais plus lues) — bouton « Ajouter un
  VPC », répété pour chaque VPC source à répliquer :
  - VPC source, issu du scan du compte (NetId + tag Name), sélection unique
    par bloc
  - **AZ cible**, propre à ce VPC (les subnets/VMs de VPC différents du même
    plan peuvent être créés dans des AZ différentes)
  - Bouton pour créer le VPC cible à l'identique (même IpRange), sur le
    compte source (même région) ou le compte cible (autre région)
  - Liste de VMs de ce VPC, issue du scan du compte, case à cocher pour les
    sélectionner
    - Si une VM précédemment cochée n'existe plus : alerte sur le job et surlignage en rouge dans la liste de VMs
    - Ordre de démarrage à la bascule (optionnel, par VM, plan-level — voir *Bascule PRA*, s'applique quel que soit le VPC de la VM)
  - Bouton pour retirer ce VPC de la configuration (supprime aussi son VPC
    cible et ses VM cible, best-effort)
- **Net Peering** : si plusieurs VPC source du plan sont peerés entre eux
  côté source, le peering équivalent est **détecté et recréé
  automatiquement** côté cible à la création d'un VPC cible et à chaque
  resynchronisation (`app/target.py::sync_net_peerings`) — aucune
  configuration manuelle, purement dérivé de l'état du compte source. Les
  routes des tables de routage qui pointent vers ce peering sont recréées
  dans la foulée si les deux VPC cible existent déjà.
- Fréquence de snapshot
- Nombre de snapshots à conserver sur la production
- Bouton pour supprimer le plan de reprise (avec double validation)
- Bouton pour exporter le journal des opérations

#### 2 - Désactivation d'un Plan de reprise

- Proposition de supprimer les snapshots source
- Proposition de supprimer les VMs cible

#### 3 - Suppression d'un plan de reprise

- Proposition de suppression des snapshots source
- Proposition de suppression du VPC cible et de tous ses objets
- Proposition de suppression des snapshots cible (si autre région)
- Proposition de suppression des VMs cible (incluant les BSU)

### Zone Monitoring

- Visualisation des jobs et de leur état
- Dashboard et liste des plans : date/heure et statut de la dernière
  exécution de chaque plan (Réussi / En cours / Erreur — en erreur dès
  qu'une VM du dernier cycle a échoué), badge Test PRA / Basculé si une
  bascule est en cours ou effectuée (voir *Bascule PRA*)
- Alerte sur les jobs avec une AK proche de sa fin de validité
- Journal d'audit (connexions, actions de modification) — zone admin >
  Sécurité

## Versioning et mises à jour

- SemVer (`MAJOR.MINOR.PATCH`), version courante trackée dans le fichier
  `VERSION` à la racine du dépôt (texte brut, ex. `0.1.0`) — première version
  publiée : `0.1.0`.
  - `PATCH` : correctif sans changement de comportement.
  - `MINOR` : nouvelle fonctionnalité rétrocompatible.
  - `MAJOR` : breaking change assumé (voir politique de compatibilité
    ci-dessous — à éviter autant que possible, donc rare tant que le produit
    n'a pas atteint `1.0.0`).
  - La version est affichée en haut à droite de chaque vue de l'application
    (`app/templates/base.html`, bloc `.hdr-version`), lue une fois au
    démarrage par `app/version.py::current_version()` et exposée à tous les
    templates via `templates.env.globals["app_version"]`
    (`app/templates_env.py`).
- Un bandeau « Mise à jour disponible » s'affiche à côté de la version si une
  release GitHub plus récente existe (`app/version.py::available_update()`) :
  interroge `GET /repos/glorp-fr/osc-pra/releases/latest`, résultat mis en
  cache 6h pour ne pas solliciter l'API à chaque requête, échec silencieux
  (offline, GitHub injoignable...) — l'absence de bandeau ne garantit donc pas
  que l'app est à jour, seulement qu'aucune mise à jour n'a pu être détectée.
- Branches Git :
  - `main` : version déployée chez les clients. Ne reçoit que des merges
    depuis `dev` (release) ou des hotfixes ponctuels — toujours dans un état
    déployable. Chaque merge vers `main` s'accompagne d'un bump de `VERSION`
    et d'un tag `vX.Y.Z` (+ release GitHub correspondante), c'est ce tag que
    `update.sh` cible par défaut.
  - `dev` : intégration des évolutions en cours, base des branches de
    fonctionnalité.
- Politique de compatibilité (à vérifier à chaque évolution avant de merger
  vers `dev`/`main`) :
  - Schéma DB : uniquement des migrations additives (nouvelle colonne via
    `EXPECTED_COLUMNS`/`_migrate_schema`, voir `app/db.py`) — jamais de
    suppression/renommage de colonne existante sans plan de migration
    explicite, une base SQLite existante chez un client doit continuer à
    fonctionner après `update.sh`.
  - Routes/URLs existantes : pas de changement de comportement ou de
    suppression sans réflexion préalable (scripts cron déjà déployés,
    éventuels appels externes).
  - Configuration existante (`settings`, `plans`, variables d'environnement,
    fichier `VERSION` lui-même) : doit rester valide après mise à jour, sans
    intervention manuelle autre qu'`update.sh`.
- Mise à jour d'une instance déployée : script `update.sh` à la racine du
  dépôt — fetch des tags, `git checkout` du dernier tag `vX.Y.Z` (ou d'un tag
  précis passé en argument, y compris pour un rollback vers un tag
  antérieur), mise à jour des dépendances Python (`pip install -r
  requirements.txt`), redémarrage du service systemd `osc-pra`. Refuse de
  s'exécuter si des modifications locales non commitées existent. Les
  migrations de schéma s'appliquent automatiquement au démarrage de l'app
  (`init_db()`, appelé par `app/main.py` au `startup`) — rien à faire
  manuellement de ce côté.
- Mise à jour depuis l'UI : bouton « Mettre à jour maintenant » (zone admin
  > Paramètres, section *Mise à jour*, admins uniquement), actif dès qu'une
  mise à jour est détectée. Exécute `update.sh` (vers le dernier tag,
  aucun choix de tag depuis l'UI — rollback vers un tag antérieur toujours
  possible en SSH via `./update.sh <tag>`) et journalise chaque étape comme
  un job (`scripts/run_update.py`, `job_type = "update"`), consultable
  depuis la page Paramètres. Lancé via `sudo systemd-run --unit=... --collect`
  (`app/routers/admin.py::mise_a_jour_lancer`) plutôt qu'un simple
  sous-processus de l'app : `update.sh` redémarre `osc-pra.service`, qui
  tourne en `KillMode=control-group` (par défaut) — un sous-processus normal
  serait tué par ce redémarrage avant d'avoir fini de journaliser le
  résultat. Mécanisme vérifié en conditions réelles (survit à un vrai
  redémarrage du service) avant intégration.

## Déploiement

Distributions supportées : Debian, Ubuntu (script basé sur apt).

Script de setup qui :

- Installe toutes les dépendances
- Crée le compte admin avec un mot de passe demandé à l'exécution
- Demande si il faut activer HTTPS
  - Demande de FQDN
  - Mise en place de Let's Encrypt
  - Alerte sur ouverture des ports HTTP et HTTPS dans le security group
- Sécurise le serveur (voir *Sécurité du déploiement* ci-dessous)

## Sécurité du déploiement

Mis en place par `setup.sh` sur une installation neuve ; déployé
manuellement le 2026-07-30 sur les instances déjà existantes (à refaire à
la main sur toute instance installée avant cette date).

- **fail2ban** : jail `sshd` standard (backend `systemd`, pas de
  `/var/log/auth.log` sur une install Debian/Ubuntu récente — uniquement
  journald) ; jail `osc-pra-login` maison
  (`/etc/fail2ban/filter.d/osc-pra-login.conf`) qui bannit une IP après 5
  échecs de connexion en 15 min (1h de ban), en lisant directement le
  journal du service (`journalmatch = _SYSTEMD_UNIT=osc-pra.service`,
  regex sur les lignes d'accès uvicorn `POST /login ... 401`). Attention
  au préfixe ajouté par le backend systemd de fail2ban (hostname + nom de
  service) : le filtre ne doit pas ancrer `^` en tout début de ligne.
  `fail2ban-client status osc-pra-login` pour voir les IP actuellement
  bannies ; `fail2ban-client set osc-pra-login unbanip <ip>` pour débannir.
- **ufw** : politique par défaut refus en entrée / autorisation en
  sortie, seuls 22 (SSH, `limit` — throttle après trop de tentatives),
  80 (HTTP) et 443 (HTTPS) autorisés en entrée. Vient en plus du security
  group Outscale de la VM (deux couches de pare-feu), pas à sa place —
  les deux doivent laisser passer 22/80/443.
- **unattended-upgrades** : déjà actif par défaut sur une image Debian
  standard (mises à jour de sécurité automatiques) — vérifié, pas
  d'action supplémentaire du setup sur ce point.
- Authentification SSH par mot de passe désactivée (`PasswordAuthentication
  no` dans `sshd_config`) — attendu déjà en place sur l'image de base,
  pas géré par `setup.sh`.
