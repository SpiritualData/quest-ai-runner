"""The choose-what-to-index function. Every solution is ONE function with the signature of the library's gate:

    gate(walk_root, skip_dirs, provider, model, cards_dir) -> FolderGate

``walk_root`` is a corpus root, ``skip_dirs`` the folder names the walk never enters, ``provider`` the model
provider the folder review may call (None when no model can be asked), ``cards_dir`` the lane's card store,
where the cached verdicts (folder_review.json) live. It returns a ``context_risk_gate.FolderGate``: the folders
of the corpus that no card source may read (the file walk, nested store imports, ancestor imports, mounts).

The scorer installs the function at the one seam every card source asks (``folder_gate`` as imported by the
bootstrap and the walk) and runs the library's real ``FileContextStore.bootstrap`` over a fixture corpus, so what
is measured is what a lane would import, mount and walk, not what the function says about itself.

An implementation module in ``impls/`` exposes ``build(meter) -> gate``. No solution here calls a model of its
own: the review's answers come from the reviewer the case names.
"""
