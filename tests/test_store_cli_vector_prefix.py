"""``store-info --vector-location``: an empty ``--vector-collection-prefix`` falls back to QAR's default collection prefix."""
import argparse

from quest_ai_runner.store_cli import vector_location_from_args


def args(prefix):
    return argparse.Namespace(vector_location="http://qdrant.invalid:6333", vector_size=384, vector_collection_prefix=prefix,
                              embedder_id="test-embedder", vector_path=None, cards_dir="/tmp/cards")


def test_an_empty_prefix_falls_back_to_the_default_and_a_given_one_is_kept():
    assert vector_location_from_args(args(""))[0]["collection_prefix"] == "qar_ctx"
    assert vector_location_from_args(args("mine"))[0]["collection_prefix"] == "mine"
