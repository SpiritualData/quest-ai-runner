"""FLOOR: ignores the query and returns the user's three most recently updated conversations. What
"just give it the latest" scores; real recall must beat it."""
ALTERNATIVE = True


def build(meter):  # noqa: ARG001
    def recall(query, user, world):  # noqa: ARG001
        mine = sorted((c for c in world["conversations"] if c["user_id"] == user), key=lambda c: -c["updated_at"])
        return {"ids": [c["id"] for c in mine[:3]]}
    return recall
