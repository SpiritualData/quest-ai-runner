These are ESOI records rendered as a diagram (evolvable structure that optimizes intent). This page is generated from the solutions' outputs and the implementations' `uses_solution_ids` by `esoi diagram`; edit those records, not this page.

# Solution diagram: Quest AI Runner

Quest AI Runner turns what a Quest user asks into work that moves their intents, and Quest Self is the one assistant that does it. Quest Self asks Quest Consciousness who is asking and what they are growing toward, reads their account and their own material through Quest Body, reasons about the request through Intelligence and reaches what lies outside through World. Quest Consciousness takes each request in through Quest Requests, hands it to the representative that fits through Quest Representatives, asks a person only for what is theirs through Quest Decision Requests, learns from feedback through Feedback Learning and proposes what is most likely to work for their intents through Intent Evolution. Quest Body keeps the person's Quest data in Quest Context, changes it exactly as asked through Quest Change Executor and keeps what is said about an account true through Quest Account Truth. Intelligence plans each request through Mind, recalls what it needs through Subconscious, keeps the long view through Higher Mind with Guidance and Quest Intents, and holds the work to a measure through Critical Thinking with Goal Completion and Response Quality. World asks models through Model Plumbing with Model Tier Chooser and Vendor Model Caller, uses outside tools through Tools with Delegated Agent Sessions and File Editing, and answers from public research through Science. Nervous System watches every run, Intake Sources brings requests in from where people work and Integrity keeps every part a specified solution that is measured.

An arrow runs from the solution that provides to the solution that builds on it; its label is what the provider outputs.

```mermaid
flowchart LR
    s0["Integrity<br/><i>system-integrity</i>"]
    s1["Intake Sources<br/><i>intake-sources</i>"]
    s2["Nervous System<br/><i>nervous-system</i>"]
    s3["Vendor Model Caller<br/><i>vendor-model-caller</i>"]
    s4["Science<br/><i>the-science</i>"]
    s5["Critical Thinking<br/><i>critical-thinking</i>"]
    s6["Goal Completion<br/><i>goal-completion</i>"]
    s7["Response Quality<br/><i>response-quality</i>"]
    s8["guidance"]
    s9["Tools<br/><i>the-tools</i>"]
    s10["Quest Context<br/><i>quest-context</i>"]
    s11["Quest Requests<br/><i>quest-requests</i>"]
    s12["Quest Self<br/><i>quest-self</i>"]
    s13["Quest Body<br/><i>quest-body</i>"]
    s14["Image description for grounding<br/><i>describe-an-attachment</i>"]
    s15["Request intake<br/><i>take-in-the-world</i>"]
    s16["Context choice<br/><i>choose-the-context</i>"]
    s0 --> s12
    s0 --> s15
    s1 --> s9
    s1 --> s15
    s2 --> s12
    s3 --> s2
    s3 --> s9
    s3 --> s11
    s3 --> s12
    s3 --> s14
    s4 --> s12
    s5 --> s12
    s6 --> s2
    s6 --> s5
    s7 --> s0
    s7 --> s5
    s8 --> s5
    s10 -->|"Evidence from the person's own sources, with where each item came from"| s4
    s10 -->|"Evidence from the person's own sources, with where each item came from"| s15
    s10 -->|"Evidence from the person's own sources, with where each item came from"| s16
    s11 -->|"The request taken in once and its outcome reported"| s5
    s11 -->|"The request taken in once and its outcome reported"| s12
    s12 -->|"The assistant's answer and the changes it made"| s9
    s13 --> s8
    s13 --> s11
    s13 --> s12
```
