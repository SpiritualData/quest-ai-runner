"""The card cache is shared by every account on a corpus, so its files take the umask's mode and not mkstemp's 0600."""
import os
import stat

from quest_ai_runner.the_body.internal_context.card_store.card_cache_file import FileCardCache


def test_the_cache_file_follows_the_umask_not_mkstemp(tmp_path):
    old = os.umask(0o002)
    try:
        cache = FileCardCache(str(tmp_path / "cache"))
        assert cache.write_json("listing.json", {"cards": {}})
    finally:
        os.umask(old)
    mode = stat.S_IMODE(os.stat(tmp_path / "cache" / "listing.json").st_mode)
    assert mode & 0o060 == 0o060, oct(mode)
