"""FLOOR: ignores the question and returns the first three documents by id, with no read. What chance and a
tidy corpus alone score; anything real must beat it."""
ALTERNATIVE = True


def build(meter):  # noqa: ARG001
    def retrieve(question, corpus):  # noqa: ARG001
        return {"ids": sorted(d["id"] for d in corpus["docs"])[:3], "texts": {}}
    return retrieve
