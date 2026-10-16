# Reading files on another machine: `serve-files`

A brain that runs on one machine (a hosted Quest backend, say) cannot read the files of another.
`quest-ai-runner serve-files` fixes that without copying anything: the team runs a small, read-only
file server next to its files, and the brain reads through it with `RemoteFilesAdapter`. The server
uses only the Python standard library.

## Run it

```bash
export QAR_FILES_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
quest-ai-runner serve-files --root /path/to/the/files --port 8765
```

* `--root` (or `QAR_FILES_ROOT`): the one directory tree that is served. Nothing outside it is
  reachable.
* `QAR_FILES_TOKEN`: the bearer token every request must carry (at least 16 characters). The
  server **refuses to start without it**, and the token is never accepted on the command line.
* `--host` (`QAR_FILES_HOST`, default `127.0.0.1`) and `--port` (`QAR_FILES_PORT`, default `8765`).

The server speaks plain HTTP and binds to loopback by default. To reach it from another machine put
TLS in front of it (a reverse proxy that forwards one path to this port) and keep the token secret.
Run it as a service with a systemd unit whose `EnvironmentFile` holds the token and root.

## What it offers

All operations are `POST /v1/<op>` with a JSON body and `Authorization: Bearer <token>`.
`GET /v1/health` answers without a token and reveals nothing.

| Operation | Body | Answer |
|---|---|---|
| `search` | `pattern` (regex), `scope?`, `max_hits?` | matching lines: path, line number, text |
| `find` | `pattern` (name part or `*.glob`), `scope?`, `max_results?` | files, newest first |
| `list` | `path?` | the entries of one directory |
| `read` | `path`, `start_line?`, `end_line?`, `heading?`, `max_bytes?` | text; pdf, docx and xlsx are converted to text; other binary files answer with a note |

There is no write, delete, rename or execute operation, and no way to add one by request.

## Safety rules

* **Containment.** The same `resolve_in_tree` the rest of the library uses: `..`, absolute paths
  outside the root and symlinks that lead outside the root are refused. Searches never follow
  symlinked directories, and a symlinked file is read only when its real target is inside the root.
* **Secret files.** Names matching `.env*`, `*.key`, `*.pem`, `*secret*`, `*credential*`,
  `*password*`, SSH and cloud credential folders, private-key and keystore files, and data files
  named like a token or API key (`*.json`, `*.yaml`, `*.txt` and so on) are invisible: not
  listed, found, searched or read. The rule is applied to every folder name on the path. A file that
  looks like a credential (a private key block, a `"private_key"` entry) is refused even when its
  name is harmless.
* **Redaction.** Obvious secret values in ordinary files (private key blocks, well-known API key
  shapes, bearer tokens, `password = <long value>`) are replaced with `[redacted]` in every answer.
  This is a safety net, not a guarantee; keep real secrets out of the served tree.
* **Bounded work.** Request, response and file sizes, search time and file count, and the number of
  operations running at once are capped. A search that hits a cap says so.
* **Hidden folders.** Searches and `find` skip dot-folders and the usual build folders
  (`node_modules`, virtual environments); name one as the scope to search inside it.

## Using it from a brain

```python
from quest_ai_runner.adapters import CompositeRetrievalAdapter, FilesAdapter, RemoteFilesAdapter

remote = RemoteFilesAdapter(alias="server", base_url="https://files.example.org/prefix",
                            token=token, description="the team's file server")
retrieval = CompositeRetrievalAdapter([FilesAdapter(docs_root), remote])
```

The alias namespaces every path (`server:docs/guide.md`), so several servers can sit in one
composite. The planner uses its ordinary read vocabulary:

* read: `{"rel_path": "server:docs/guide.md", "start_line": 1, "end_line": 80}` or `"heading": "Setup"`
* search: `{"grep": "regex", "scope": "server:docs"}` (a scope is required, because a composite
  broadcasts every grep to every member and a remote search is never free)
* find: `{"query": {"files": "server", "op": "find", "pattern": "*.md", "scope": "docs"}}`
* list: `{"query": {"files": "server", "op": "list", "path": "docs"}}`

A server that cannot be reached, refuses the token or times out comes back as an error observation
that names the problem and never the token or the URL. Redirects are not followed.

## Why not MCP

The library has an MCP client (`MCPRetrievalAdapter`), and an MCP file server would have been the
standard-looking choice. It was not used because that adapter cannot grep and reads only listed
resources, so the planner would lose `grep(pattern, scope)` and `read_section(path, lines)`, and an
MCP server would make every file host install the pinned `mcp` package. A small JSON API keeps the
host dependency-free and the planner's vocabulary unchanged.
