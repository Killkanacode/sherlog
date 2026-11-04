"""How much Sherlog may do on its own.

  L0  suggest only: Sherlog never acts
  L1  every action waits for a human to approve it (the demo default)
  L2  low-risk rollbacks, flag flips and restarts run on their own when the
      diagnosis is confident enough; anything else still waits for approval

`dry_run` makes every approved action a no-op that is only recorded. Both
start from the environment and can be changed at runtime (PUT /settings);
the latest choice is kept in Sherlog's database.

Whatever the model says, an action Sherlog proposes must also pass `vet`:
it has to target a service involved in the incident, and a rollback, config
revert or flag flip needs the matching change in the evidence. Automatic runs
are throttled (`throttle`): a few per hour and a cooldown per service.
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from sherlog import db
from sherlog.config import settings

LEVELS = ("L0", "L1", "L2")
AUTO_ACTIONS = ("rollback", "flag_off", "restart")


@dataclass
class Policy:
    autonomy: str = settings.autonomy if settings.autonomy in LEVELS else "L1"
    dry_run: bool = settings.dry_run

    def decide(self, action: dict, confidence: float) -> str:
        """'suggest', 'approve' or 'auto' for an action that passed the whitelist."""
        if self.autonomy == "L0":
            return "suggest"
        if (
            self.autonomy == "L2"
            and action["type"] in AUTO_ACTIONS
            and action["risk"] == "low"
            and confidence >= settings.auto_min_confidence
        ):
            return "auto"
        return "approve"

    def to_dict(self) -> dict:
        return {**asdict(self), "auto_min_confidence": settings.auto_min_confidence}

    async def load(self) -> None:
        stored = await db.get_setting("policy") or {}
        self.autonomy = stored.get("autonomy", self.autonomy)
        self.dry_run = stored.get("dry_run", self.dry_run)

    async def update(self, autonomy: str | None = None, dry_run: bool | None = None) -> dict:
        if autonomy is not None:
            if autonomy not in LEVELS:
                raise ValueError(f"autonomy must be one of {', '.join(LEVELS)}")
            self.autonomy = autonomy
        if dry_run is not None:
            self.dry_run = dry_run
        await db.put_setting("policy", {"autonomy": self.autonomy, "dry_run": self.dry_run})
        return self.to_dict()


def vet(evidence: dict | None, services: list[str], type_: str, params: dict) -> str | None:
    """Why an action Sherlog proposes doesn't fit the incident, or None."""
    evidence = evidence or {}
    involved = set(services) | set(evidence.get("suspect_services") or [])
    if "postgresql" in involved:
        involved.add("postgres")  # the service graph calls it postgresql, the platform postgres
    target = params.get("service")
    if target and target not in involved:
        return f"{target} is not involved in this incident"
    changes = evidence.get("changes") or []
    if type_ == "rollback" and not any(c.get("type") == "deploy" and c.get("service") == target for c in changes):
        return f"there is no recent deploy of {target} to roll back"
    if type_ == "config_revert" and not any(c.get("type") == "config" and c.get("service") == target for c in changes):
        return f"there is no recent config change on {target} to revert"
    if type_ == "flag_off" and not any(
        c.get("type") == "flag" and (c.get("details") or {}).get("flag") == params.get("flag") for c in changes
    ):
        return f"flag {params.get('flag')} was not changed recently"
    return None


def throttle(executed: list[dict], service: str | None, now: datetime) -> str | None:
    """Why an automatic run must wait for a human instead: `executed` are the
    actions run in the last hour (who ran them in `approved_by`)."""
    auto = [a for a in executed if a.get("approved_by") == "sherlog"]
    if len(auto) >= settings.max_auto_actions_per_hour:
        return f"{len(auto)} automatic actions in the last hour (limit {settings.max_auto_actions_per_hour})"
    cooldown = timedelta(seconds=settings.service_cooldown_s)
    for a in executed:
        if service and a["params"].get("service") == service and now - datetime.fromisoformat(a["executed_at"]) < cooldown:
            return f"{service} was changed less than {settings.service_cooldown_s // 60} min ago"
    return None


policy = Policy()
