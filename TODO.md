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
  `jobs`). Pas de vue centralisée dans l'UI.
- Définir ce qu'on veut tracer en plus de l'historique des jobs déjà en
  base : appels octl (succès/échec, durée), connexions/actions admin
  (audit trail), erreurs applicatives.
- Rotation et rétention des logs (`data/cron.log` n'est aujourd'hui
  jamais purgé).
- Éventuellement : une page « Logs » dans la zone admin, avec filtre par
  plan/type/statut (au-delà de ce que `/suivi` affiche déjà pour les
  jobs).

## Gestion des mises à jour

- Pas de mécanisme de mise à jour de l'application aujourd'hui (déploiement
  manuel : `git pull` + redémarrage du service — voir README, section
  *Exploitation*).
- Vérifier/documenter une procédure de mise à jour propre : migration de
  schéma (`app/db.py` gère déjà l'ajout de colonnes via
  `EXPECTED_COLUMNS`, à étendre si besoin), redémarrage sans perte de job
  en cours, rollback en cas de souci.
- Suivi de version d'`octl` : le setup installe la dernière release au
  moment de l'install, mais rien ne vérifie ensuite si une mise à jour
  d'octl est disponible/nécessaire, ni ne fixe une version minimale
  compatible.
- Éventuellement : affichage de la version de l'app / d'octl dans l'UI
  (page Paramètres).

## Corrections et ajustements UI

- **Erreur sur les security groups au resync** : signalée le 2026-07-29,
  pas encore reproduite/diagnostiquée précisément — à creuser (concerne
  probablement `sync_target_network` / `create_security_group_rule` dans
  `app/target.py` et `app/octl.py`, ajoutés le même jour). Récupérer le
  message d'erreur exact affiché sur la page Visualiser (bouton "Mettre à
  jour depuis la source") pour diagnostiquer.
- **Modifier le menu de gauche (zone admin)** : demandé le 2026-07-29,
  sans détail sur le changement attendu — à préciser (quelles entrées,
  quel regroupement, lié à l'ouverture de la gestion des plans aux
  opérateurs ?).

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
