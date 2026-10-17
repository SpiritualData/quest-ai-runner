"""The conformance scenarios, shared by every backend: each takes a harness (see ``backends.py``) and the case
``args`` and returns what it observed. The backends differ only in which harness they are given."""
import threading

FIELDS = ("name", "summary", "keywords")


def card(card_id, version=1, text="harbour notes"):
    return {"id": card_id, "name": f"Card {card_id}", "summary": f"{text} v{version} for {card_id}",
            "description": f"{text} v{version} for {card_id}", "keywords": [card_id, text.split()[0]],
            "content": [{"id": "c1", "type": "note", "locator": {"text": f"{text} {card_id}"}, "why": "why"}]}


def same(stored, original):
    """The stored card holds every field the card was written with (a backend may add its own stamps)."""
    return isinstance(stored, dict) and all(stored.get(k) == original.get(k) for k in FIELDS + ("content",))


def roundtrip(harness, args):
    repo = harness.repo()
    cards = {cid: card(cid) for cid in ("alpha", "beta", "gamma")}
    wrote = [repo.write(cid, c) for cid, c in cards.items()]
    loaded = repo.load_all()
    return {"writes_ok": all(wrote), "read_equal": all(same(repo.read(c), cards[c]) for c in cards),
            "load_all_ids": sorted(loaded), "load_all_equal": all(same(loaded.get(c), cards[c]) for c in cards),
            "exists": all(repo.exists(c) for c in cards)}


def overwrite(harness, args):
    repo = harness.repo()
    repo.write("a", card("a", 1))
    repo.write("b", card("b", 1))
    repo.write("a", card("a", 2, text="harbour notes"))
    loaded = repo.load_all()
    return {"count": len(loaded), "ids": sorted(loaded), "newest": same(repo.read("a"), card("a", 2)),
            "newest_in_listing": same(loaded.get("a"), card("a", 2)), "neighbour_untouched": same(loaded.get("b"), card("b", 1))}


def no_loss(harness, args):
    n = int(args["n"])
    repo = harness.repo()
    wrote = sum(bool(repo.write(f"card-{i:04d}", card(f"card-{i:04d}", text=f"topic{i} notes"))) for i in range(n))
    loaded = repo.load_all()
    want = {f"card-{i:04d}" for i in range(n)}
    return {"writes_ok": wrote == n, "lost": len(want - set(loaded)), "extra": len(set(loaded) - want),
            "count": len(loaded)}


def no_duplicate(harness, args):
    repo = harness.repo()
    for _ in range(int(args["repeats"])):
        repo.write("same", card("same"))
    repo.write("other", card("other"))
    loaded = repo.load_all()
    return {"count": len(loaded), "ids": sorted(loaded), "same_once": sum(1 for c in loaded.values() if c.get("id") == "same")}


def delete(harness, args):
    repo = harness.repo()
    for cid in ("a", "b", "c"):
        repo.write(cid, card(cid))
    removed = repo.delete("b")
    repo.load_all()
    gone = {"read_none": repo.read("b") is None, "exists_false": repo.exists("b") is False,
            "listing_ids": sorted(repo.load_all())}
    missing_ok = repo.delete("never-written")
    twice_ok = repo.delete("b")
    after = harness.restart(repo)
    return {"delete_true": removed is True, "read_none": gone["read_none"], "exists_false": gone["exists_false"],
            "listing_ids": gone["listing_ids"], "missing_ok": missing_ok is True, "twice_ok": twice_ok is True,
            "restart_ids": sorted(after.load_all())}


def concurrent(harness, args):
    repo = harness.repo()
    threads_n, per = int(args["threads"]), int(args["per_thread"])
    failures = []

    def work(t):
        for i in range(per):
            cid = f"t{t}-{i}"
            if not repo.write(cid, card(cid)):
                failures.append(cid)
            if not repo.write("shared", card("shared", version=t * 100 + i)):
                failures.append("shared")

    pool = [threading.Thread(target=work, args=(t,)) for t in range(threads_n)]
    for th in pool:
        th.start()
    for th in pool:
        th.join()
    loaded = repo.load_all()
    want = {f"t{t}-{i}" for t in range(threads_n) for i in range(per)} | {"shared"}
    shared = loaded.get("shared") or {}
    whole = bool(shared) and shared.get("summary") == shared.get("description") and \
        shared.get("summary", "").endswith("for shared")
    return {"write_failures": len(failures), "lost": len(want - set(loaded)), "extra": len(set(loaded) - want),
            "shared_once": sum(1 for c in loaded.values() if c.get("id") == "shared"), "shared_whole": whole}


def restart(harness, args):
    repo = harness.repo()
    cards = {f"r{i}": card(f"r{i}") for i in range(int(args["n"]))}
    for cid, c in cards.items():
        repo.write(cid, c)
    repo.delete("r0")
    second = harness.restart(repo)
    loaded = second.load_all()
    return {"ids_equal": sorted(loaded) == sorted(set(cards) - {"r0"}),
            "fields_equal": all(same(loaded.get(c), cards[c]) for c in cards if c != "r0"),
            "read_after_restart": same(second.read("r1"), cards["r1"])}


def revision(harness, args):
    repo = harness.repo()
    first = repo.revision()
    repo.write("a", card("a"))
    second = repo.revision()
    repo.delete("a")
    third = repo.revision()
    return {"changes_on_write": first != second, "changes_on_delete": second != third}


def tolerance(harness, args):
    repo = harness.repo()
    return {"read_missing": repo.read("nope"), "exists_missing": repo.exists("nope"), "empty_listing": repo.load_all()}


KINDS = {"roundtrip": roundtrip, "overwrite": overwrite, "no_loss": no_loss, "no_duplicate": no_duplicate,
         "delete": delete, "concurrent": concurrent, "restart": restart, "revision": revision,
         "tolerance": tolerance}
