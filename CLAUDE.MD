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
- VPC source, issu du scan du compte (NetId + tag Name), sélection unique
- Bouton pour créer le VPC cible à l'identique (même IpRange), sur le compte
  source (même région) ou le compte cible (autre région)
- Cible de réplication
  - Même région
  - Autre région (liste de choix des régions Outscale)
    - AK/SK
    - Région
    - Bucket S3 de synchro
    - AK/SK S3
    - Endpoint S3 défini selon la région choisie
    - Nombre de snapshots à conserver côté cible (à demander à l'utilisateur)
- Mise à jour du VPC cible automatique (case à cocher, activée par défaut) :
  reconstruit les objets manquants sur le VPC cible à chaque synchronisation
  planifiée (voir *Mécanisme de réplication* ci-dessus)
- Liste de VMs, issue du scan du compte, case à cocher pour les sélectionner
  - Si une VM précédemment cochée n'existe plus : alerte sur le job et surlignage en rouge dans la liste de VMs
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
  qu'une VM du dernier cycle a échoué)
- Alerte sur les jobs avec une AK proche de sa fin de validité

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

## Déploiement

Distributions supportées : Debian, Ubuntu (script basé sur apt).

Script de setup qui :

- Installe toutes les dépendances
- Crée le compte admin avec un mot de passe demandé à l'exécution
- Demande si il faut activer HTTPS
  - Demande de FQDN
  - Mise en place de Let's Encrypt
  - Alerte sur ouverture des ports HTTP et HTTPS dans le security group
