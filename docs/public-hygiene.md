# Keeping a public repository clean

This repository is public, and so may be a consumer's mirror of its own integration. Nothing
consumer specific or secret may be in the files or the history: API keys and tokens, real machine
paths, object ids, addresses of real people. `esoi hygiene` (the `esoi` package, a dev dependency) is the scan that
enforces it.

    esoi hygiene            # every file git knows about under the current folder
    esoi hygiene --staged   # only what is staged, for a pre-commit hook
    esoi hygiene --root path/to/repo
    esoi hygiene --history  # also every line ever added in git history

Exit 0 means clean, 1 means findings. Each finding prints `path:line: pattern: first characters...`;
the whole secret is never printed.

## What it looks for

Anthropic, OpenAI, Quest, AWS and GitHub credentials, bearer tokens, private key blocks with a
real-length body, `/home/<name>/`, `/Users/<name>/` and `C:\Users\<name>\` paths, 24-hex object ids
and email addresses.

## What it lets through

Documentation must be able to show the SHAPE of a secret, so these are not findings: `qsk_...`
and `sk-ant-...` in prose, `/home/user/` and `/home/<name>/`, addresses at `example.*` and other
placeholder domains, role mailboxes such as `security@`, a 40-hex commit id, a key header over a stub
body, and filler ids of repeated digits.

## What it cannot do

It reads the working files unless `--history` is given. A secret that was ever committed must be rotated;
removing it from the files is not enough. A new kind of secret is a new pattern in esoi's hygiene scanner and a
new planted case in `evaluation/components/keep_secrets_out/cases.json`, in the same change.
