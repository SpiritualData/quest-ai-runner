# Idea rating scale

The shared scale for every `kind=idea` stage in the cascading conditionals of this repository's metrics. An idea stage
needs no implementation: before an alternative is built, someone (a person or an AI) answers the five questions below
about its written description and rates each answer from 0 to 4. The stage's number is the sum of the ratings divided by
20 (five questions times a top rating of 4), a fraction from 0 to 1. It passes when it is at least 0.75, which is
the stage's `min-pass-rate`, so on average the answers are a 3 ("mostly") or better.

## The questions

1. **Problem fit**: does the idea address the problem the solution exists for?
2. **Purpose fit**: does it serve the people or systems the solution's purpose names, in the situations it names?
3. **Real conditions**: would it work against the real inputs and services the purpose involves, without relying on a stand-in?
4. **Cost and risk**: is what it costs to run, and what could go wrong, small enough for its benefit?
5. **Checkability**: can a result of its work be told right or wrong later, by this metric's later stages?

## What each rating means

| Rating | Meaning |
|---|---|
| 0 | No. The description contradicts the question, or says nothing that could answer it. |
| 1 | Barely. A reader can argue for it, but a gap or conflict is evident. |
| 2 | Partly. Some of the question is answered, and a named part is open. |
| 3 | Mostly. The question is answered, with a small open point written down. |
| 4 | Fully. The question is answered with reasons, and no open point is known. |

Rate each question separately, write one sentence of reason beside each rating, and record the reasons with the
number where the alternative is proposed. A rating below 2 on any question is a reason to revise the idea before the
stage is passed, whatever the average.
