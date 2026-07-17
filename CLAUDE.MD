# Osc-PRA

Projet d'application permettant de créer et gérer des Plans de Reprise d'Activité (PRA) sur Outscale.

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
2. Création d'une VM avec la même configuration dans le VPC de PRA
3. Restauration des disques depuis les snapshots
4. Attachement des disques dans le même ordre que sur la VM source

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
- Base de données : SQLite (fichier unique embarqué, sans serveur à gérer)
  - *Remplace le stockage JSON brut initialement prévu : les jobs sont traités en parallèle (un job par VM), donc des écritures concurrentes sur un fichier JSON risquent la corruption. SQLite apporte les transactions ACID et le verrouillage nécessaires, tout en restant simple à déployer/sauvegarder (fichier unique)*
- Utilisation d'OCTL
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

### Zone Opérateur

#### 1 - Création de Plan de reprise

- Nom du plan
- Définition AK/SK
- Cible de réplication
  - Même région
  - Autre région (liste de choix des régions Outscale)
    - AK/SK
    - Région
    - Bucket S3 de synchro
    - AK/SK S3
    - Endpoint S3 défini selon la région choisie
    - Nombre de snapshots à conserver côté cible (à demander à l'utilisateur)
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
- Alerte sur les jobs avec une AK proche de sa fin de validité

## Déploiement

Script de setup qui :

- Installe toutes les dépendances
- Crée le compte admin avec un mot de passe demandé à l'exécution
- Demande si il faut activer HTTPS
  - Demande de FQDN
  - Mise en place de Let's Encrypt
  - Alerte sur ouverture des ports HTTP et HTTPS dans le security group
