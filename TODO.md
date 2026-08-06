# TODO

Chantiers identifiés, à prioriser.

## ~~Gestion d'un PRA multi VPC~~ — fait le 2026-07-31

- Un plan de reprise peut désormais répliquer plusieurs VPC source (table
  `plan_vpcs`, voir `app/plan_vpcs.py` et CLAUDE.MD section *Création de
  Plan de reprise*) : AZ cible par VPC, VPC cible par VPC, Bascule PRA et
  Sandbox couvrant tous les VPC du plan en une seule fois (ordre de
  démarrage déjà plan-level, EIP/NAT par VPC). Net Peering entre les VPC du
  plan détecté côté source et recréé automatiquement côté cible
  (`app/target.py::sync_net_peerings`) — jamais exécuté contre un compte
  réel, voir *Statut expérimental* dans CLAUDE.MD. Migration additive
  automatique des plans mono-VPC existants au démarrage.

## Mise en place du reporting

- Alerting par email sur les événements importants : job de snapshot/
  restauration en erreur, AK/SK invalide ou proche de sa fin de validité
  (déjà détecté et affiché sur `/suivi`, mais pas encore notifié).
  Prévu dans CLAUDE.MD (« Alerting / reporting par email », section
  *Zone admin*) — paramètres SMTP / email source / email cible déjà
  présents dans le schéma `settings`, non exploités.
- Export du journal des opérations d'un plan (bouton déjà présent dans
  l'UI mais désactivé — `plans.html`, « Exporter le journal »).
- Rapport périodique (ex. hebdomadaire) récapitulant l'état des plans et
  des derniers points de restauration.

## Mise en place des logs

- Aujourd'hui : logs applicatifs uniquement via `journalctl -u osc-pra`
  (service systemd) et `data/cron.log` (stdout/stderr des jobs cron,
  peu utilisé car les erreurs sont surtout stockées dans la table
  `jobs`). Pas de vue centralisée dans l'UI pour ces deux-là.
- ~~Connexions/actions admin (audit trail)~~ — fait le 2026-07-30 :
  table `audit_log` (`app/db.py`), écrite par `app/audit.py::log_event`,
  consultée dans la zone admin > **Sécurité** (`/admin/securite`,
  réservée aux admins). Trace les connexions/déconnexions/échecs de
  connexion et les actions de modification (comptes, plans, paramètres,
  resync manuel, lancement manuel d'un plan, sauvegarde manuelle,
  resync du crontab) avec utilisateur, IP, date/heure et détail. Reste
  à tracer : appels octl (succès/échec, durée), erreurs applicatives.
- Rotation et rétention des logs (`data/cron.log` n'est aujourd'hui
  jamais purgé ; `audit_log` non plus — pas de purge/rétention définie).
- Éventuellement : une page « Logs » dans la zone admin, avec filtre par
  plan/type/statut (au-delà de ce que `/suivi` affiche déjà pour les
  jobs, et de ce que `/admin/securite` affiche pour l'audit).

## Gestion des mises à jour

- ~~Pas de mécanisme de mise à jour de l'application~~ — fait le 2026-07-30 :
  versioning SemVer (fichier `VERSION`, `0.1.0`), script `update.sh`
  (checkout du dernier tag `vX.Y.Z`, dépendances, redémarrage du service),
  version + bandeau « mise à jour disponible » (release GitHub) affichés en
  haut à droite de chaque vue. Détails et politique de compatibilité dans
  CLAUDE.MD, section *Versioning et mises à jour*.
- ~~Créer réellement la branche `dev`~~ — fait le 2026-07-30, poussée sur
  origin. ~~Poser un premier tag~~ — fait : `v0.1.1` et `v0.2.0` posés et
  publiés (release GitHub) sur `main`.
- Bouton « Mettre à jour maintenant » ajouté le 2026-07-30 (zone admin >
  Paramètres) — voir CLAUDE.MD, section *Versioning et mises à jour*. Testé
  en isolant le point qui posait problème (permissions git/systemd-run,
  voir historique git), **pas encore observé de bout en bout via un vrai
  clic sur le bouton en conditions réelles** (le dernier test réel a servi
  à corriger le bug de permissions, pas à valider un update.sh complet
  réussi) — à surveiller au premier usage réel.
- Rollback testé en conditions réelles (`update.sh <tag antérieur>`) —
  toujours pas fait, ni depuis l'UI (le bouton ne propose pas de choisir un
  tag) ni en SSH.
- Suivi de version d'`octl` : le setup installe la dernière release au
  moment de l'install, mais rien ne vérifie ensuite si une mise à jour
  d'octl est disponible/nécessaire, ni ne fixe une version minimale
  compatible.

## Corrections et ajustements UI

- ~~Erreur sur les security groups au resync~~ — corrigé le 2026-07-29
  (FromPortRange=0 mal géré par la forme "tableau" des paramètres octl
  pour les règles tcp/udp, voir historique git).
- **Modifier le menu de gauche (zone admin)** : demandé le 2026-07-29,
  sans détail sur le changement attendu — à préciser (quelles entrées,
  quel regroupement, lié à l'ouverture de la gestion des plans aux
  opérateurs ?).

## Reprise des caractéristiques de la VM source (demandé le 2026-07-30)

- ~~Reprise des tags de la VM source sur la VM cible (ex. tag `Name`)~~ —
  fait le 2026-07-30 : tous les tags source sont désormais repris (VPC,
  subnets, security groups, route tables, VMs, volumes cible), pas
  seulement Name ; remis en phase à chaque cycle pour les VMs.
- Reprise de l'IP privée de la VM source sur la VM cible.
- OMI de la VM cible : ne plus réutiliser directement l'`ImageId` de la
  VM source (cf. bug OMI supprimée, section *Connu, non planifié*), mais
  choisir une OMI générique selon le type d'OS de la VM (Linux, Windows,
  RedHat).

## Bascule PRA et Sandbox (demandé le 2026-07-30)

- ~~Activation PRA / Test de PRA~~ — fait le 2026-07-30 : démarrage des VM
  cible dans un ordre configurable par plan (`plans.vm_restart_order`),
  bascule/allocation des EIP et reconstruction de la NAT Gateway côté
  cible (`app/failover.py`, `scripts/run_bascule.py`,
  `scripts/end_test.py`). Voir CLAUDE.MD, section *Bascule PRA*.
- ~~Sandbox~~ — fait le 2026-07-30 : clone d'un VPC indépendant du VPC de
  PRA persistant du plan, construit comme un Test de PRA
  (`scripts/run_sandbox.py`), listé/géré depuis le menu **Sandbox**
  (`/admin/sandbox`).
- **Important — non testé en conditions réelles** : les fonctions octl
  EIP (`CreatePublicIp`, `LinkPublicIp`, `UnlinkPublicIp`,
  `DeletePublicIp`) et NAT Gateway (`CreateNatService`,
  `DeleteNatService`) sont toutes neuves — leur syntaxe vient de `octl
  iaas api <Action> --help`, mais aucune n'a encore été exécutée contre
  un compte Outscale réel. À valider via un **Test de PRA** avant toute
  **Activation PRA** réelle sur un plan de production.
- Reste à faire : bouton « Basculer vers le PRA » remplacé par les deux
  nouveaux boutons, mais pas de bascule automatique/déclenchée par une
  panne détectée (action manuelle uniquement, volontaire) ; pas de
  reprise de l'IP privée à la bascule (même limite que la sync normale) ;
  la bascule/le sandbox ne gèrent que la cible « même région » (cross-
  région hérite de la même limite que la sync normale).

## Choix du snapshot à restaurer (demandé le 2026-08-06)

- Aujourd'hui, le snapshot utilisé pour restaurer une VM cible n'est
  jamais un choix de l'opérateur :
  - **Sandbox** (`scripts/run_sandbox.py`) : le snapshot le plus récent de
    chaque volume est pris automatiquement (`octl.list_snapshots` trié par
    `CreationDate` décroissant, `[0]`).
  - **Cycle de sync normal** (`scripts/run_plan.py`) : un nouveau snapshot
    est créé à chaque cycle et restauré immédiatement — toujours le plus
    récent par construction.
  - **Bascule PRA (Activation / Test)** (`scripts/run_bascule.py`) : pas de
    restauration du tout, la VM cible démarre telle quelle (déjà tenue à
    jour par le cycle de sync normal) — aucune notion de snapshot dans ce
    flux actuellement.
- Demandé : au lancement d'une Bascule, d'un Test ou d'un Sandbox, pouvoir
  choisir un snapshot antérieur (point de restauration) pour une VM
  plutôt que de toujours reprendre l'état courant/le plus récent — utile
  par exemple pour revenir avant une corruption ou un incident connu à
  une date précise.
- Implique : lister les snapshots disponibles par volume dans l'UI (déjà
  fait côté octl via `list_snapshots`, juste à exposer) avec leur date ;
  si le snapshot choisi n'est pas celui déjà utilisé/le plus récent,
  déclencher une restauration des volumes de la VM cible depuis ce
  snapshot précis avant de démarrer la VM — même mécanisme de
  restauration BSU que la sync normale et le sandbox
  (`app/restore.py::restore_vm`), mais piloté par un choix explicite
  plutôt que "toujours le plus récent". Pour la Bascule PRA, ça ajoute une
  étape de restauration qui n'existe pas du tout dans ce flux aujourd'hui.

---

## Connu, non planifié ici (pour mémoire)

Limitations déjà identifiées dans le code (`app/restore.py`,
`scripts/run_plan.py`) et non couvertes par les points ci-dessus :

- Réplication cross-région (export/import de snapshot via S3).
- Resync réseau : routes vers une passerelle VPN non répliquées (ressource
  hors scope) ; EIP et NAT Gateway volontairement exclus du resync, repris
  seulement à la bascule (voir *Bascule PRA et Sandbox* ci-dessous, fait le
  2026-07-30). Les Net Peering entre VPC d'un même plan, eux, SONT
  répliqués depuis la gestion du multi-VPC (fait le 2026-07-31, voir
  section dédiée ci-dessus) — plus une limitation.
- Restauration BSU : ne gère que les devices déjà présents sur la VM
  cible lors d'un cycle de mise à jour (un nouveau volume attaché côté
  source après la création de la VM cible n'est pas répliqué
  automatiquement).
- ~~Bascule (basculement vers le PRA) non implémentée~~ — fait le
  2026-07-30, voir section *Bascule PRA et Sandbox* ci-dessous. L'IP
  privée de la VM source n'est toujours pas reprise sur la VM cible (déjà
  listé plus haut, section *Reprise des caractéristiques de la VM
  source*) ; la bascule cross-région reste non gérée (dépend de
  l'export/import S3, non implémenté).
- **Bug réel découvert le 2026-07-29, plan test-snc** : la création de la
  VM cible échoue avec `The ImageId 'ami-5d7dbfbb' doesn't exist.` — l'AMI
  utilisée au lancement de la VM source a depuis été supprimée/dérégistrée
  (la VM source continue de tourner dessus normalement, mais on ne peut
  plus lancer une *nouvelle* VM depuis cette même AMI). `restore.py`
  utilise directement `source_vm["ImageId"]`, qui n'est donc pas fiable
  dans la durée. Piste : générer une image à partir de la VM source
  (`CreateImage`) au moment de la restauration plutôt que de réutiliser
  l'AMI d'origine — mais ça ajoute un cycle d'attente (image "available")
  et un nettoyage des images obsolètes à gérer.
- Les appels octl de création/modification de VM, de volume et de
  ressources réseau (`CreateVms`, `CreateVolume`, `LinkVolume`,
  `CreateSecurityGroupRule`, `CreateRouteTable`, `CreateInternetService`...)
  n'ont été vérifiés qu'en syntaxe (`octl --dry-run`), jamais exécutés
  contre l'API réelle par Claude — mais CreateVms l'a été par un
  utilisateur réel (voir bug ci-dessus), donc le chemin d'exécution jusqu'à
  cet appel est confirmé fonctionnel.
