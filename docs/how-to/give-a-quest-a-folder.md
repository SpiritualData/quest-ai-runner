# Give a quest a folder

An autopilot run for a quest that has no folder in the lane's folder map starts by giving it one. A
quest that is already mapped is unchanged.

## Turn it on

In the lane's `qar.toml`:

```toml
quest_folder_map_file = "/path/to/quest_folders.json"   # {quest_id: folder}
new_quest_folder_root = "/path/to/stories"              # optional; default <corpus_root>/stories
```

A new folder is created only inside `new_quest_folder_root`. The lane never writes outside it.

## What the run does

The brief opens with a "FIRST STEP: give this quest a folder" section naming the quest and two commands:

```bash
# Read only: folders that look like a quest home (CLAUDE.md, QUEST_SYNC.md or GOALS.md) and which quest each is bound to
quest-ai-runner quest-folder candidates --config /path/to/qar.toml

# The one safe write
quest-ai-runner quest-folder bind --config /path/to/qar.toml --quest-id QUEST_ID --folder /path/to/stories/<area>/<slug>
```

The run picks a candidate dedicated to the same area and quest, never one bound to another quest.
Otherwise it creates `<new_quest_folder_root>/<area>/<slug>`, reusing an existing area name. `bind`
adds one entry to the map file (under a lock, through a temp file and a rename, so no other entry is
dropped), creates the folder, scaffolds its zones and syncs the quest into it in the lane's
`quest_folder_sync_direction`. It refuses a folder bound to another quest, a quest already bound
elsewhere, and a folder outside the root. Binding the same pair again changes nothing.

## When it takes effect

The poller re-reads the map file every scan and hands the new map to the autopilot, so the next run
and every later one use the folder with no restart. The run that binds it works from the folder in
the same run, because its brief says so.

From Python: `quest_folder_bind.bind_quest_folder(map_file, quest_id, folder, root=root, client=client)`.
Never edit the map file by hand or rebuild this in a script.
