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

---

## Connu, non planifié ici (pour mémoire)

Limitations déjà identifiées dans le code (`app/restore.py`,
`scripts/run_plan.py`) et non couvertes par les points ci-dessus :

- Réplication cross-région (export/import de snapshot via S3).
- Réplication des règles de security group et des tables de routage
  (seuls les subnets et security groups eux-mêmes sont resynchronisés).
- Restauration BSU : ne gère que les devices déjà présents sur la VM
  cible lors d'un cycle de mise à jour (un nouveau volume attaché côté
  source après la création de la VM cible n'est pas répliqué
  automatiquement).
- Les appels octl de création/modification de VM et de volume
  (`CreateVms`, `CreateVolume`, `LinkVolume`...) n'ont été vérifiés qu'en
  syntaxe (`octl --dry-run`), jamais exécutés en conditions réelles.
