# TODO

Chantiers identifiés, à prioriser.

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
- Reste à faire : créer réellement la branche `dev` sur le dépôt, poser le
  premier tag `v0.1.0` sur `main`, et rollback testé en conditions réelles
  (`update.sh <tag antérieur>`) — pas encore fait, seul le mécanisme est en
  place.
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

---

## Connu, non planifié ici (pour mémoire)

Limitations déjà identifiées dans le code (`app/restore.py`,
`scripts/run_plan.py`) et non couvertes par les points ci-dessus :

- Réplication cross-région (export/import de snapshot via S3).
- Resync réseau : routes vers un Net peering ou une passerelle VPN non
  répliquées (ressources elles-mêmes hors scope) ; EIP et NAT Gateway
  volontairement exclus du resync, repris seulement à la bascule (non
  implémentée — voir point ci-dessous).
- Restauration BSU : ne gère que les devices déjà présents sur la VM
  cible lors d'un cycle de mise à jour (un nouveau volume attaché côté
  source après la création de la VM cible n'est pas répliqué
  automatiquement).
- **Bascule (basculement vers le PRA) non implémentée** : la VM cible est
  créée/tenue à jour à l'arrêt, mais rien ne démarre automatiquement le
  failover — c'est aujourd'hui une action manuelle hors de l'app
  (démarrer la VM cible, réassocier l'EIP). C'est aussi à cette étape que
  l'EIP source devrait être détachée de la VM source et réattachée à la
  VM cible (même région) ou qu'une nouvelle EIP devrait être allouée et
  associée (autre région).
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
