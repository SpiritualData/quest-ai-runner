These are ESOI records rendered as a diagram (evolvable structure that optimizes intent). This page is generated from the solutions' outputs and the implementations' `uses_solution_ids` by `esoi diagram`; edit those records, not this page.

# Solution diagram: Quest Body

Quest Body is the form of the assistant and the Quest data that belongs to the person. Quest Body reads and writes Quest only through one client and declares the research search the host offers. Quest Context gathers what the person's own quests, notes, decisions and teammates say, and keeps it inside the person's scope. Quest Change Executor changes a quest exactly as the person asked, through Delegated Agent Sessions and File Editing when the work is more than one step. Quest Account Truth makes what the assistant says about an account the account as it is now.

An arrow runs from the solution that provides to the solution that builds on it; its label is what the provider outputs.

```mermaid
flowchart LR
    s0["Delegated Agent Sessions<br/><i>delegated-agent-sessions</i>"]
    s1["File Editing<br/><i>file-editing</i>"]
    s2["Quest Account Truth<br/><i>quest-account-truth</i>"]
    s3["Quest Change Executor<br/><i>quest-change-executor</i>"]
    s0 --> s2
    s1 --> s2
    s3 -->|"The change made and the state read back"| s2
```
