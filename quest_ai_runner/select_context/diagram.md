These are ESOI records rendered as a diagram (evolvable structure that optimizes intent). This page is generated from the solutions' outputs and the implementations' `uses_solution_ids` by `esoi diagram`; edit those records, not this page.

# Solution diagram: Selecting the context a planner reads

select-context chooses the cards, files and conversations a planner reads before it plans, so the right material is found however a request is worded. It follows the pointer of a card to the person's current notes and files through resolve-references, and it searches the cards and the specification records of a repository in one ranking through search-cards-and-specs.

An arrow runs from the solution that provides to the solution that builds on it; its label is what the provider outputs.

```mermaid
flowchart LR
    s0["Context card selector<br/>select-context"]
    s1["Fail-safe card reference resolver<br/>resolve-references"]
    s2["Unified card and specification search<br/>search-cards-and-specs"]
    s1 -->|"Rendered text for the pointer, possibly marked abridged, or..."| s0
    s2 -->|"One assembled context: ranked items, each with its kind..."| s0
```
