"""A fake hub used only to prove that a new hub needs registry data or configuration and no edit to the library."""


class FakeSearch:
    """A search backend that no library file knows about; built from the environment like any other."""
    name = "fakehub"

    def __init__(self, base_url):
        self.base_url = base_url

    @classmethod
    def from_env(cls, env):
        url = env.get("FAKEHUB_URL", "")
        return cls(url) if url else None
