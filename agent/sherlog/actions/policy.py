"""How much Sherlog may do on its own.

  L0  suggest only: Sherlog never acts
  L1  every action waits for a human to approve it (the demo default)
  L2  low-risk rollbacks, flag flips and restarts run on their own when the
      diagnosis is confident enough; anything else still waits for approval

`dry_run` makes every approved action a no-op that is only recorded. Both
start from the environment and can be changed at runtime (PUT /settings);
the latest choice is kept in Sherlog's database.
"""

from dataclasses import asdict, dataclass

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


policy = Policy()
