"""One measurable AI increment on a quest's milestone, for a turn whose resolved intent is progress on it ("get X moving"): the flow step that replaced the
owned-next-step write.

Role: QAR's wiring of the hub's ``CriticalThinkingComposition.progress`` for the milestones a quest keeps in Quest. A milestone is never waiting on a person:
the turn always makes something the AI can do itself and shows it measured before and after. The steps, each decided on structured fields:

1. **Work items.** One cheap call decomposes the milestone's Solution record into work items (``kind``, ``actor``, ``inputs`` as record ids, ``outward`` when
   the item publishes, sends or pays, ``moves_parent``, ``requirements``); a second, independent call says for each whether the AI can do it now with reading
   and writing only. An item is the AI's only when both agree and code finds its inputs and no outward act (the hub's ``doable_by_ai``).
   ONE AI item is then pinned by structure: what the done test is made of (things the team makes, a decision, or what other people do; a yes/no metric is a
   decision by code) fixes its kind, the decomposition only fills in its details, and once its rubric is kept it stays the item until the goal holds every
   unit it planned or the done test changes.
2. **Metric upkeep** (hub ``metric-upkeep``): when every item that moves the milestone's own metric is a person's, one intermediate metric the AI can move is
   added through ``apply_spec_edit``, after Decision Check agrees it tracks the done test.
3. **The increment** (hub ``choose_increment``), measured BEFORE on what the milestone's keyed deliverable goal already holds (nothing scores 0), then made by
   one writing call grounded in the turn's reads (each claim checked by ``grounded_text``), then measured AFTER by a scoring call that is not the writer
   (a requirement counts as met only with a quote code finds in the part).
4. **Kept** with ``keep_milestone_deliverable`` on the keyed goal ``<milestone> deliverables`` (updated in place on a repeat), the increment's readings in the
   call, and the milestone's metric **read again** through ``read_spec_records``.

The reply is composed by code from those results: the deliverable, the increment before and after, the milestone metric read again, then at most ONE step only
a person can do. A failure to read or write is a ``LaneGap`` with the tool's own words, never an empty result.

Layout: this file is the entry point and re-exports the names the milestone flow and its tests import. The code sits in five sibling modules, each with
one scoped purpose: ``milestone_facts`` (constants, record and kept-goal readers, the spec-edit writer), ``milestone_rubric`` (the rubric, the scorer, the
done-test class, the deliverable's solution), ``milestone_depth`` (sentence-support checks and the writer, evidence and depth prompts),
``milestone_ladder`` (``make_progress``, the flow of one turn) and ``milestone_reply`` (``Progress`` and the reply composed from it).
"""
from __future__ import annotations

from .milestone_facts import (  # noqa: F401
    ANSWER_LINE,
    DELIVERABLES,
    DEPTH_CALLS,
    FOLLOW_SEPARATOR,
    FOLLOW_SPLIT,
    GROUNDING_CHARS,
    KEEP_TOOL,
    SPEC_TIER,
    NO_EM_DASH,
    OUTWARD,
    OVERVIEW_HEAD,
    PART_SEPARATOR,
    PART_SPLIT,
    QUESTION_LINE,
    QUEST_METRIC_COLUMNS,
    RECORD_CHARS,
    RECORD_ID,
    SOURCE_LABEL,
    SPEC_EDIT_TOOL,
    TO_BE_DECIDED,
    ToolSpecEdit,
    WRITER_TIER_ENV,
    clip_line,
    kept_follow_on,
    kept_parts,
    log,
    parse_json,
    quest_metric_fields,
    questions_of,
    render_questions,
    squash,
    whole_row,
    with_reading_fields,
    without_ids,
    work_skill,
    writer_tier,
)
from .milestone_rubric import (  # noqa: F401
    CLASS_PICK,
    DECISION,
    DECISION_REQUIREMENT,
    EVIDENCE_REQUIREMENTS,
    HOME_PICK,
    ITEM_PICK,
    KIND_PROMPT,
    KNOWLEDGE_KIND,
    KNOWLEDGE_SOURCE,
    KeptRubric,
    MADE,
    NONE_FITS,
    OTHERS,
    RUBRIC_CHECK_PROMPT,
    RUBRIC_CLAIM,
    RUBRIC_KIND,
    RUBRIC_LINE,
    RUBRIC_PROMPT,
    SCORE_PROMPT,
    SOURCE_PROPOSAL,
    Scorer,
    YES_NO,
    confirmed_item_order,
    deliverable_home,
    done_digest,
    done_test_class,
    kept_artifacts,
    kept_rubric,
    pinned_items,
    researched_rubric,
    rubric_entry,
    with_entry,
)
from .milestone_depth import (  # noqa: F401
    ANSWER_PROMPT,
    COMBINE_PROMPT,
    CONTENT_WORD,
    DEEP_BRIEF,
    ECHO_SHARE,
    EVIDENCE_PROMPT,
    EVIDENCE_QUESTIONS,
    LINK,
    META,
    NUMBER,
    ORDINAL_LIMIT,
    PART_PROMPT,
    POINTER,
    REPHRASE_PROMPT,
    SOURCE_SHARE,
    STATUS_WORDS,
    STEP_PROMPT,
    SUPPORT_PROMPT,
    SUPPORT_VIEWS,
    WRITE_PROMPT,
    checked_line,
    deliverable_brief,
    held_source,
    kept_step,
    marked_claims,
    solution_prompt,
    stems5,
    supported_sentences,
    with_supported_markers,
    without_markers,
    without_rubric_echo,
)
from .milestone_ladder import (  # noqa: F401
    DECOMPOSE_PROMPT,
    VERIFY_PROMPT,
    dependent_item,
    first_person_step,
    make_progress,
    work_items,
)
from .milestone_reply import (  # noqa: F401
    Progress,
    milestone_pass,
)
