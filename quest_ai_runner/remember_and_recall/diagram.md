These are ESOI records rendered as a diagram (evolvable structure that optimizes intent). This page is generated from the solutions' outputs and the implementations' `uses_solution_ids` by `esoi diagram`; edit those records, not this page.

# Solution diagram: Remembering and recalling the person's own knowledge

remember-and-recall recalls the cards that match a request through card-store. card-store keeps the cards through persist-cards, serves the last known listing through cache-cards while a backend is down, finds them by meaning through index-by-meaning, labels them through label-context-cards, reports stale ones through detect-stale-cards, holds their growth in check through bound-card-growth and shrinks an inflated store through repair-a-card-store. repair-a-card-store rewrites the stored cards through persist-cards and asks choose-what-to-index which sources deserve cards.

An arrow runs from the solution that provides to the solution that builds on it; its label is what the provider outputs.

```mermaid
flowchart LR
    s0["Context card persistence backend<br/>persist-cards"]
    s1["Semantic vector index<br/>index-by-meaning"]
    s2["Source-change staleness detector<br/>detect-stale-cards"]
    s3["Offline card cache and write outbox<br/>cache-cards"]
    s4["Card store growth prevention<br/>bound-card-growth"]
    s5["Card store repair<br/>repair-a-card-store"]
    s6["Index scope decision: which folders and...<br/>choose-what-to-index"]
    s7["Card categories and metadata<br/>label-context-cards"]
    s8["QAR card repository as the hub card...<br/>card-store"]
    s0 -->|"Load-all returns a mapping of id to card, read returns a..."| s5
    s0 -->|"Load-all returns a mapping of id to card, read returns a..."| s8
    s1 -->|"Ranked hits with id, score, text and payload for search..."| s8
    s2 -->|"Whether the file is stale, and the stale card list for the..."| s8
    s3 -->|"The stored listing with its update time, or none, and the..."| s8
    s4 -->|"Cards written only under an id made of their origin; a..."| s8
    s5 -->|"A plan (tidy-state/last-plan.json for the startup tidy), a..."| s8
    s6 -->|"A FolderGate: the set of corpus-relative folders no card..."| s5
    s7 -->|"The text or verdict the call returns"| s8
```
