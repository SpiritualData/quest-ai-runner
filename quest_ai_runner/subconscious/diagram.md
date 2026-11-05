These are ESOI records rendered as a diagram (evolvable structure that optimizes intent). This page is generated from the solutions' outputs and the implementations' `uses_solution_ids` by `esoi diagram`; edit those records, not this page.

# Solution diagram: Subconscious

Subconscious recalls the cards that match a request through Hub card store. Hub card store keeps the cards through Context card persistence backend, and Context card persistence backend serves the last known listing while a backend is down with Offline card cache and write outbox. Card store repair decides which sources deserve a card by asking Index scope decision before it rewrites anything. The remaining parts of the area do not use one another in code, so they are grouped here by purpose, the care of a card store that stays current, proportionate and findable. Source-change staleness detector reports the cards that have drifted from their source file. Card store growth prevention keeps a store proportionate to its sources. Card categories and metadata gives each card its labels. QAR topic cards for a folder proposes the topic cards a lane indexes.

An arrow runs from the solution that provides to the solution that builds on it; its label is what the provider outputs.

```mermaid
flowchart LR
    s0["Context card persistence backend<br/><i>persist-cards</i>"]
    s1["Offline card cache and write outbox<br/><i>cache-cards</i>"]
    s2["Card store repair<br/><i>repair-a-card-store</i>"]
    s3["Index scope decision<br/><i>choose-what-to-index</i>"]
    s4["Hub card store<br/><i>card-store</i>"]
    s0 -->|"Load-all returns a mapping of id to card, read returns a card or nothing, write and delete return success flags, and revision returns a change stamp used to invalidate caches"| s4
    s1 -->|"The stored listing with its update time, or none, and the queued writes oldest first"| s0
    s3 -->|"A FolderGate: the set of corpus-relative folders no card source may read, and of those the risk-flagged ones that could not be reviewed, with why; every card source asks it, so its complement is the set of folders and stores allowed to produce or import cards"| s2
```
