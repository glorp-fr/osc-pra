"""Construction et lecture d'expressions cron simples pour le sélecteur de
fréquence (horaire / quotidien / hebdomadaire)."""
import re

DAY_LABELS = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
DAY_CRON_VALUES = [1, 2, 3, 4, 5, 6, 0]  # cron : 0 = dimanche
HOURLY_INTERVALS = [1, 2, 3, 4, 6, 8, 12]

DEFAULT_PARSED = {"freq_type": "quotidien", "hourly_interval": 1, "time": "02:00", "days": [1]}


def _parse_time(time_str: str) -> tuple[int, int]:
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", time_str or "")
    if not match:
        return 2, 0
    return int(match.group(1)), int(match.group(2))


def build_cron(freq_type: str, hourly_interval: int = 1, time_str: str = "02:00", days: list[int] | None = None) -> str:
    if freq_type == "horaire":
        interval = hourly_interval if hourly_interval in HOURLY_INTERVALS else 1
        return f"0 */{interval} * * *"

    hour, minute = _parse_time(time_str)

    if freq_type == "quotidien":
        return f"{minute} {hour} * * *"

    if freq_type == "hebdomadaire":
        days = [d for d in (days or [1]) if d in DAY_CRON_VALUES]
        days_csv = ",".join(str(d) for d in sorted(set(days or [1])))
        return f"{minute} {hour} * * {days_csv}"

    raise ValueError(f"Type de fréquence inconnu : {freq_type}")


def parse_cron(cron: str | None) -> dict:
    """Décode une expression cron générée par build_cron pour préremplir le formulaire."""
    if not cron:
        return dict(DEFAULT_PARSED)

    parts = cron.split()
    if len(parts) != 5:
        return dict(DEFAULT_PARSED)

    minute, hour, dom, month, dow = parts

    if hour.startswith("*/") and minute == "0" and dom == "*" and month == "*" and dow == "*":
        interval = int(hour[2:]) if hour[2:].isdigit() else 1
        return {"freq_type": "horaire", "hourly_interval": interval, "time": "02:00", "days": [1]}

    if minute.isdigit() and hour.isdigit():
        time_str = f"{int(hour):02d}:{int(minute):02d}"
        if dow == "*":
            return {"freq_type": "quotidien", "hourly_interval": 1, "time": time_str, "days": [1]}
        days = [int(d) for d in dow.split(",") if d.lstrip("-").isdigit()]
        return {"freq_type": "hebdomadaire", "hourly_interval": 1, "time": time_str, "days": days or [1]}

    return dict(DEFAULT_PARSED)


def describe_cron(cron: str | None) -> str:
    parsed = parse_cron(cron)
    if parsed["freq_type"] == "horaire":
        return f"Toutes les {parsed['hourly_interval']} heure(s)"
    if parsed["freq_type"] == "quotidien":
        return f"Tous les jours à {parsed['time']}"
    labels = [DAY_LABELS[DAY_CRON_VALUES.index(d)] for d in parsed["days"] if d in DAY_CRON_VALUES]
    return f"Chaque {', '.join(labels)} à {parsed['time']}"
