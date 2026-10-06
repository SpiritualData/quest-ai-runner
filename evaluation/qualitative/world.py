"""The disposable qualitative-eval WORLD on DEV, built only through the app's own REST endpoints.

Five quests in five domains (so "pick the right quest" is itself testable), each with an outcome,
current state, acceptance criteria, measurable outcomes, goals and notes, plus collections of every
useful shape (binary habit, timer habit, journal with custom fields, list/table tracker, expense
log, reading log) linked to DIFFERENT quests, and seeded entries/notes that carry PIVOT FACTS: one
specific fact whose presence should change the correct answer ("knee injury", "budget overrun",
"deadline moved"). Dataset authors reference those facts by name through ``PIVOTS``.

    python3 evaluation/qualitative/world.py setup      # build (idempotent: refuses if a world exists)
    python3 evaluation/qualitative/world.py teardown   # delete everything, re-fetch to prove it
    python3 evaluation/qualitative/world.py show       # print ids and pivots

Everything carries the tag ``ZZQEVAL``: quests in their acceptance criteria and current state,
collections in their description, quest-doc / team-context entries in their name. Teardown works
from the state file AND sweeps the tag, so a half-built world is still cleaned.

DEV ONLY (devclient refuses to load otherwise).
"""
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from devclient import (  # noqa: E402
    QUEST_TEAM, TAG, WORK_DIR, api, entries_of, goals_of, list_collections, list_quests,
    measurable_outcomes_of, notes_of, quest_state, unwrap_list)

STATE_PATH = WORK_DIR / "world.json"
FALLBACK_CATEGORY = "cat_df82187e53c0"  # a known dev category ("Fitness"); used when a name lookup misses

TODAY = datetime.date.today()


def ago(days, hour=8):
    """ISO timestamp ``days`` days before today."""
    return f"{(TODAY - datetime.timedelta(days=days)).isoformat()}T{hour:02d}:00:00Z"


def ahead(days):
    return (TODAY + datetime.timedelta(days=days)).isoformat()


def week_period(offset=0):
    day = TODAY + datetime.timedelta(weeks=offset)
    iso = day.isocalendar()
    return f"{iso[0]}_W{iso[1]:02d}"


MONTH_PERIOD = f"{TODAY.year}_{TODAY.month:02d}"

# ---------------------------------------------------------------------------------------------
# PIVOTS: name -> {quest_key, description, exact_values}. ``exact_values`` are strings/numbers a
# correct answer should quote or compute from. Authors reference these by name in a case's
# ``must_use_pivots``; the judge is told the description and exact values.
# ---------------------------------------------------------------------------------------------

PIVOTS = {
    "KNEE_INJURY": {
        "quest_key": "fitness",
        "where": "journal_run entry 2 days ago (notes + effort 5)",
        "description": ("A Run Log entry says the user hurt their left knee at km 6 and the physio "
                        "banned running for 10 days (cycling and swimming only). Any running plan, "
                        "mileage goal or 'run today' habit advice must account for this."),
        "exact_values": {"body_part": "left knee", "pain_at_km": 6, "run_ban_days": 10,
                         "allowed": "cycling and swimming", "physio": "Dana"},
    },
    "RACE_MOVED": {
        "quest_key": "fitness",
        "where": "fitness quest note",
        "description": ("A quest note says the target race, the Riverside 10K, moved from 8 Nov to "
                        "22 Nov. Any timeline, taper or 'weeks left' answer must use 22 Nov."),
        "exact_values": {"race": "Riverside 10K", "old_date": "8 Nov", "new_date": "22 Nov"},
    },
    "PB_TIME_TRIAL": {
        "quest_key": "fitness",
        "where": "journal_run entry 9 days ago",
        "description": ("A time-trial entry records a 10K in 54:10 (5:25 per km), the best recent "
                        "evidence of the user's real fitness versus the 50:00 target."),
        "exact_values": {"time": "54:10", "pace": "5:25", "distance_km": 10.0},
    },
    "BUDGET_OVERRUN": {
        "quest_key": "business",
        "where": "expense_log entries versus the business quest's current_state (cap 2,000 USD)",
        "description": ("Launch expenses sum to 2,340 USD against a stated 2,000 USD budget cap, "
                        "a 340 USD (17 percent) overrun. The largest line is the 900 USD label "
                        "printer order. Spending advice must start from the overrun."),
        "exact_values": {"cap_usd": 2000, "total_usd": 2340, "overrun_usd": 340,
                        "largest_item": "Label printer", "largest_usd": 900},
    },
    "SUPPLIER_DELAY": {
        "quest_key": "business",
        "where": "business quest note",
        "description": ("A note says the wax supplier slipped the shipment from 14 Oct to 28 Oct, "
                        "so the 20 Oct soft launch cannot happen on schedule."),
        "exact_values": {"supplier": "Northwick Wax", "old_date": "14 Oct", "new_date": "28 Oct",
                        "soft_launch": "20 Oct"},
    },
    "LAVENDER_DEMAND": {
        "quest_key": "business",
        "where": "customer_interviews journal entries",
        "description": ("Four of the five customer interviews asked for lavender; nobody asked for "
                        "the planned vanilla. Product and scent decisions should follow this."),
        "exact_values": {"interviews": 5, "asked_lavender": 4, "asked_vanilla": 0},
    },
    "DEADLINE_MOVED": {
        "quest_key": "research",
        "where": "research quest note",
        "description": ("A note says the advisor, Dr. Okafor, moved the methods chapter deadline "
                        "from 15 Nov to 3 Nov."),
        "exact_values": {"advisor": "Dr. Okafor", "old_date": "15 Nov", "new_date": "3 Nov"},
    },
    "SAMPLE_SIZE_DROP": {
        "quest_key": "research",
        "where": "writing_log entry (notes)",
        "description": ("A writing-session entry records that the study sample fell from 120 to 84 "
                        "participants after dropouts, which the methods section must reflect."),
        "exact_values": {"planned_n": 120, "actual_n": 84},
    },
    "CONTRADICTING_PAPER": {
        "quest_key": "research",
        "where": "reading_log entry",
        "description": ("The reading log flags a 2023 paper by Lin et al. whose finding contradicts "
                        "the chapter's core assumption (that effect sizes are stable across sites)."),
        "exact_values": {"authors": "Lin et al.", "year": 2023, "status": "contradicts"},
    },
    "PLUMBER_WINDOW": {
        "quest_key": "family",
        "where": "home_tasks entry for the plumber + family quest note",
        "description": ("The plumber is only available 14-16 Oct. Tiling cannot start before the "
                        "plumbing is done, and the in-laws arrive 20 Nov."),
        "exact_values": {"window": "14-16 Oct", "in_laws_arrive": "20 Nov"},
    },
    "PAINT_ALLERGY": {
        "quest_key": "family",
        "where": "family_meeting journal entry",
        "description": ("Mia is allergic to Sunset Coat paint, so the family agreed to use Kiwi "
                        "Low-VOC instead. Any paint purchase advice must use Kiwi Low-VOC."),
        "exact_values": {"avoid": "Sunset Coat", "use": "Kiwi Low-VOC", "person": "Mia"},
    },
    "LISBON_TRIP": {
        "quest_key": "language",
        "where": "tutor_notes journal entry + language quest note",
        "description": ("The user flies to Lisbon on 12 Dec, which makes the speaking goal real "
                        "and time-boxed; the tutor says the weak spots are the subjunctive and "
                        "ser versus estar."),
        "exact_values": {"flight_date": "12 Dec", "weak_spots": "subjunctive; ser versus estar",
                        "tutor": "Ana"},
    },
    "SPEAKING_STREAK_BROKEN": {
        "quest_key": "language",
        "where": "speaking_timer entries (4 of the last 7 days missed)",
        "description": ("The speaking-practice habit was only done on 3 of the last 7 days, and "
                        "never on the two most recent days, so the streak is 0."),
        "exact_values": {"done_last_7": 3, "streak": 0},
    },
    "TEAM_FRIDAY_RULE": {
        "quest_key": None,
        "where": "team context entry (team-wide)",
        "description": ("Team context says the test team does no deploys or launches on Fridays "
                        "and holds its weekly review on Thursdays at 4pm."),
        "exact_values": {"no_deploy_day": "Friday", "review": "Thursday 4pm"},
    },
    "DOC_BRAND_VOICE": {
        "quest_key": "business",
        "where": "business quest doc (context entry)",
        "description": ("The business quest's brand-voice doc says: warm, plain, no exclamation "
                        "marks, never say 'artisanal', sign off with 'Light on'."),
        "exact_values": {"banned_word": "artisanal", "signoff": "Light on",
                        "punctuation": "no exclamation marks"},
    },
    # IMP_ pivots: added for the implicit-context dataset (seeded in the quest notes below).
    "IMP_TUTOR_DAYS": {
        "quest_key": "language",
        "where": "language quest note",
        "description": ("A quest note says tutor Ana only teaches on Tuesday and Thursday evenings, "
                        "so any tutor session must be booked on one of those evenings."),
        "exact_values": {"tutor": "Ana", "days": "Tuesday and Thursday evenings"},
    },
    "IMP_SAM_AWAY": {
        "quest_key": "family",
        "where": "family quest note",
        "description": ("A quest note says Sam, who does the tiling and painting, is away for work "
                        "from 26 to 30 Oct, so nothing Sam does can be scheduled in that window."),
        "exact_values": {"person": "Sam", "away": "26-30 Oct"},
    },
}


# ---------------------------------------------------------------------------------------------
# The specification: quests, goals, collections (with seeded entries), notes, docs.
# ---------------------------------------------------------------------------------------------

def habit_spec(name, description, quests, entries, timer_minutes=None):
    spec = {"name": name, "type": "habit", "description": description,
            "linked": quests, "entries": entries}
    if timer_minutes:
        spec["habit_type"] = "timer"
        spec["timer_minutes"] = timer_minutes
    else:
        spec["habit_type"] = "binary"
    return spec


QUESTS = {
    "fitness": {
        "category_names": ["Fitness", "Health & Fitness"],
        "outcome": "Run a sub-50-minute 10K at the Riverside 10K race",
        "acceptance_criteria": "A timed 10K under 50:00 recorded on a watch at the race",
        "current_state": "Currently running 10K in about 58 minutes, three runs a week",
        "timeline_days": 60,
        "measurable_outcomes": [
            "Complete eight consecutive weeks of three runs per week",
            "Record a 10K time under 52:00 as a checkpoint",
        ],
        "goals": [
            ("Build weekly mileage to 40 km", week_period(0), "week", "Measured on the running watch"),
            ("Run one tempo session per week", week_period(0), "week", "Pace under 5:10 per km"),
            ("Do a 10K time trial", week_period(1), "week", "Flat route, watch timed"),
            ("Complete the October long-run block", MONTH_PERIOD, "month", "Longest run 16 km"),
        ],
        "notes": [
            ("Race moved: the Riverside 10K is now 22 Nov instead of 8 Nov. Organisers emailed today.",
             "RACE_MOVED"),
            ("Bought new shoes, 8 mm drop, felt fine on a short test.", None),
        ],
    },
    "business": {
        "category_names": ["Career", "Finance", "Career Development"],
        "outcome": "Launch the Tiny Blue Mug candle shop and reach 100 orders in the first 60 days",
        "acceptance_criteria": "100 paid orders and a 4,000 USD revenue run rate by day 60",
        "current_state": ("Soft launch planned for 20 Oct. Launch budget cap is 2,000 USD. "
                          "Product: soy candles, scent still undecided"),
        "timeline_days": 75,
        "measurable_outcomes": [
            "Reach 100 paid orders within 60 days of launch",
            "Keep total launch spend within the 2,000 USD budget cap",
            "Complete 10 customer interviews before launch",
        ],
        "goals": [
            ("Order wax and wicks", week_period(0), "week", "Order confirmed with ship date"),
            ("Build the Shopify product page", week_period(1), "week", "Live with 3 photos"),
            ("Run 10 customer interviews", week_period(0), "week", "Ten logged interviews"),
            ("Soft launch to friends and family", MONTH_PERIOD, "month", "20 first orders"),
        ],
        "notes": [
            ("Supplier update: Northwick Wax moved our shipment from 14 Oct to 28 Oct. The 20 Oct "
             "soft launch can not happen as planned.", "SUPPLIER_DELAY"),
            ("Chose the name Tiny Blue Mug and registered the domain.", None),
        ],
        "docs": [
            ("Brand voice", "Brand voice for Tiny Blue Mug: warm, plain, no exclamation marks. "
             "Never say 'artisanal'. Sign every customer message off with 'Light on'.",
             "DOC_BRAND_VOICE"),
        ],
    },
    "research": {
        "category_names": ["Education", "Creative Expression", "Creativity"],
        "outcome": "Finish a full draft of the methods chapter of my dissertation",
        "acceptance_criteria": "A 6,000 word methods chapter that my advisor accepts for review",
        "current_state": ("About 2,400 words drafted. Sampling and instruments sections done, "
                          "analysis plan not started"),
        "timeline_days": 45,
        "measurable_outcomes": [
            "Reach 6,000 words in the methods chapter",
            "Get written feedback from the advisor on a full draft",
        ],
        "goals": [
            ("Write the analysis plan section", week_period(0), "week", "1,500 words"),
            ("Redo the participant flow figure", week_period(1), "week", "Figure matches the final N"),
            ("Read 6 papers on cross-site effect sizes", MONTH_PERIOD, "month", "Six logged readings"),
        ],
        "notes": [
            ("Dr. Okafor moved the methods chapter deadline from 15 Nov to 3 Nov after the committee "
             "calendar changed.", "DEADLINE_MOVED"),
            ("Zotero library is now organised by chapter.", None),
        ],
    },
    "family": {
        "category_names": ["Family"],
        "outcome": "Renovate the kids' bathroom before the in-laws visit",
        "acceptance_criteria": "New tiles, painted walls and working plumbing, finished before 20 Nov",
        "current_state": ("Demolition done. Plumbing not started. Tiles chosen but not bought. "
                          "In-laws arrive 20 Nov"),
        "timeline_days": 50,
        "measurable_outcomes": [
            "Finish the bathroom at least 3 days before the in-laws arrive",
            "Keep the renovation within the 1,800 USD family budget",
        ],
        "goals": [
            ("Book the plumber", week_period(0), "week", "Confirmed appointment"),
            ("Buy tiles and grout", week_period(1), "week", "38 sq ft plus 10 percent spare"),
            ("Paint the walls", MONTH_PERIOD, "month", "Two coats, low-VOC paint"),
        ],
        "notes": [
            ("The plumber can only come 14-16 Oct. Tiling has to wait for that.", "PLUMBER_WINDOW"),
            ("Heads up: Sam is away for work 26-30 Oct, so no tiling or painting those days.",
             "IMP_SAM_AWAY"),
        ],
    },
    "language": {
        "category_names": ["Education", "Growth"],
        "outcome": "Hold a 10-minute conversation in Portuguese before the Lisbon trip",
        "acceptance_criteria": "A 10-minute spoken conversation, rated B1 or better by my tutor",
        "current_state": ("Around A2. Can order food and introduce myself. Weak on verb tenses. "
                          "Practising irregularly"),
        "timeline_days": 70,
        "measurable_outcomes": [
            "Practise speaking for 20 minutes on 5 days each week",
            "Learn 300 new words from the vocabulary log",
        ],
        "goals": [
            ("Do three tutor sessions this month", MONTH_PERIOD, "month", "Three booked and attended"),
            ("Speak for 20 minutes five days this week", week_period(0), "week", "Timer log shows 5 days"),
            ("Learn the present subjunctive", week_period(1), "week", "Ten correct sentences"),
        ],
        "notes": [
            ("Flights booked: I fly to Lisbon on 12 Dec. That is the real deadline.", "LISBON_TRIP"),
            ("Ana only teaches on Tuesday and Thursday evenings, so book sessions on those days.",
             "IMP_TUTOR_DAYS"),
        ],
    },
}

# Collections. ``entries`` are (days_ago, hour, field_values). Each collection gets a stable key.
COLLECTIONS = {
    "habit_run": habit_spec(
        "Morning Run", "did I run this morning", ["fitness"],
        [(1, 7, {"status": "yes"}), (2, 7, {"status": "yes"}), (3, 7, {"status": "yes"})]),
    "timer_stretch": habit_spec(
        "Stretching Timer", "daily stretching, 15 minutes", ["fitness"], [], timer_minutes=15),
    "journal_run": {
        "name": "Run Log", "type": "journal", "description": "every run or ride, with effort",
        "linked": ["fitness"],
        "fields": [
            {"id": "distance_km", "name": "Distance km", "type": "number", "required": True},
            {"id": "effort", "name": "Effort", "type": "rating", "rating_min": 1, "rating_max": 5},
            {"id": "notes", "name": "Notes", "type": "multiline"},
        ],
        "entries": [
            (1, 8, {"distance_km": 8.2, "effort": 4, "notes": "Easy aerobic run, legs felt strong"}),
            (2, 8, {"distance_km": 6.0, "effort": 5,
                    "notes": "Sharp pain in my left knee at km 6, had to walk home. Physio Dana says "
                             "no running for 10 days, cycling and swimming only"}),
            (4, 8, {"distance_km": 12.5, "effort": 3, "notes": "Long run in the park, felt great"}),
            (9, 8, {"distance_km": 10.0, "effort": 5,
                    "notes": "10K time trial: 54:10, pace 5:25 per km, flat route"}),
            (12, 8, {"distance_km": 5.0, "effort": 2, "notes": "Recovery jog"}),
        ],
    },
    "expense_log": {
        "name": "Launch Expenses", "type": "tracker", "description": "everything spent on the launch",
        "linked": ["business"],
        "fields": [
            {"id": "item", "name": "Item", "type": "text", "required": True},
            {"id": "amount_usd", "name": "Amount USD", "type": "number", "required": True},
            {"id": "category", "name": "Category", "type": "text"},
            {"id": "vendor", "name": "Vendor", "type": "text"},
        ],
        "entries": [
            (20, 10, {"item": "Label printer", "amount_usd": 900, "category": "equipment",
                      "vendor": "PrintWorks"}),
            (18, 10, {"item": "Starter wax and wicks", "amount_usd": 420, "category": "materials",
                      "vendor": "Northwick Wax"}),
            (15, 10, {"item": "Jars and lids", "amount_usd": 380, "category": "materials",
                      "vendor": "GlassCo"}),
            (12, 10, {"item": "Shopify plan, 3 months", "amount_usd": 195, "category": "software",
                      "vendor": "Shopify"}),
            (10, 10, {"item": "Product photography", "amount_usd": 300, "category": "marketing",
                      "vendor": "Studio Lena"}),
            (6, 10, {"item": "Domain and email", "amount_usd": 45, "category": "software",
                     "vendor": "Namecheap"}),
            (3, 10, {"item": "Packaging boxes", "amount_usd": 100, "category": "materials",
                     "vendor": "BoxCo"}),
        ],
    },
    "customer_interviews": {
        "name": "Customer Interviews", "type": "journal", "description": "notes from each interview",
        "linked": ["business"],
        "fields": [
            {"id": "customer", "name": "Customer", "type": "text", "required": True},
            {"id": "scent_wanted", "name": "Scent wanted", "type": "text"},
            {"id": "insight", "name": "Insight", "type": "multiline"},
        ],
        "entries": [
            (8, 17, {"customer": "Priya", "scent_wanted": "lavender",
                     "insight": "Would buy lavender for bedtime, finds vanilla too sweet"}),
            (7, 17, {"customer": "Marcus", "scent_wanted": "lavender",
                     "insight": "Wants lavender, would pay 24 USD for a large jar"}),
            (6, 17, {"customer": "Jo", "scent_wanted": "cedar",
                     "insight": "Likes cedar, said lavender is fine too"}),
            (5, 17, {"customer": "Alba", "scent_wanted": "lavender",
                     "insight": "Lavender please, gives gifts in sets of three"}),
            (4, 17, {"customer": "Tom", "scent_wanted": "lavender",
                     "insight": "Lavender, and a refill option"}),
        ],
    },
    "writing_log": {
        "name": "Writing Sessions", "type": "journal", "description": "dissertation writing sessions",
        "linked": ["research"],
        "fields": [
            {"id": "words", "name": "Words written", "type": "number", "required": True},
            {"id": "minutes", "name": "Minutes", "type": "number"},
            {"id": "section", "name": "Section", "type": "text"},
            {"id": "notes", "name": "Notes", "type": "multiline"},
        ],
        "entries": [
            (11, 9, {"words": 600, "minutes": 90, "section": "Sampling", "notes": "Finished the sampling text"}),
            (8, 9, {"words": 450, "minutes": 75, "section": "Instruments", "notes": "Scale reliability paragraph"}),
            (5, 9, {"words": 300, "minutes": 60, "section": "Participants",
                    "notes": "Sample fell from 120 to 84 participants after dropouts, rewrote the "
                             "participants subsection and the flow figure caption"}),
            (2, 9, {"words": 520, "minutes": 100, "section": "Instruments", "notes": "Added pilot study details"}),
        ],
    },
    "reading_log": {
        "name": "Reading List", "type": "tracker", "description": "papers for the methods chapter",
        "linked": ["research"],
        "fields": [
            {"id": "title", "name": "Title", "type": "text", "required": True},
            {"id": "authors", "name": "Authors", "type": "text"},
            {"id": "year", "name": "Year", "type": "number"},
            {"id": "verdict", "name": "Verdict", "type": "text"},
            {"id": "takeaway", "name": "Takeaway", "type": "multiline"},
        ],
        "entries": [
            (14, 20, {"title": "Power analysis in multi-site studies", "authors": "Gomez & Reid",
                      "year": 2021, "verdict": "supports",
                      "takeaway": "Effect sizes were stable across sites in their sample"}),
            (10, 20, {"title": "Instrument validity under translation", "authors": "Haddad",
                      "year": 2022, "verdict": "neutral", "takeaway": "Back-translation recommended"}),
            (3, 20, {"title": "Site heterogeneity and effect size drift", "authors": "Lin et al.",
                     "year": 2023, "verdict": "contradicts",
                     "takeaway": "Found effect sizes vary a lot across sites, contradicts the "
                                 "stability assumption in my chapter"}),
        ],
    },
    "home_tasks": {
        "name": "Bathroom Tasks", "type": "tracker", "description": "task list for the bathroom job",
        "linked": ["family"],
        "fields": [
            {"id": "task", "name": "Task", "type": "text", "required": True},
            {"id": "owner", "name": "Owner", "type": "text"},
            {"id": "status", "name": "Status", "type": "text"},
            {"id": "cost_usd", "name": "Cost USD", "type": "number"},
            {"id": "due", "name": "Due", "type": "text"},
        ],
        "entries": [
            (15, 12, {"task": "Demolition", "owner": "Sam", "status": "done", "cost_usd": 150, "due": "done"}),
            (9, 12, {"task": "Plumber: move the drain", "owner": "Sam", "status": "blocked",
                     "cost_usd": 450, "due": "14-16 Oct, plumber available only then"}),
            (7, 12, {"task": "Buy tiles: matte green 4x4, 38 sq ft", "owner": "Mia", "status": "todo",
                     "cost_usd": 380, "due": "before 25 Oct"}),
            (5, 12, {"task": "Buy paint", "owner": "Sam", "status": "todo", "cost_usd": 90,
                     "due": "before 1 Nov"}),
            (3, 12, {"task": "Tile the walls", "owner": "Sam", "status": "todo", "cost_usd": 0,
                     "due": "after plumbing"}),
        ],
    },
    "family_meeting": {
        "name": "Family Meeting Notes", "type": "journal", "description": "weekly family meeting",
        "linked": ["family"],
        "fields": [
            {"id": "topic", "name": "Topic", "type": "text", "required": True},
            {"id": "decision", "name": "Decision", "type": "multiline"},
        ],
        "entries": [
            (6, 19, {"topic": "Paint choice",
                     "decision": "Mia is allergic to Sunset Coat paint. Agreed to use Kiwi Low-VOC instead"}),
            (6, 19, {"topic": "Chores", "decision": "Rotate dishes weekly"}),
        ],
    },
    "speaking_timer": habit_spec(
        "Portuguese Speaking Practice", "speaking aloud, 20 minutes", ["language"],
        [(3, 19, {"status": "yes"}), (5, 19, {"status": "yes"}), (7, 19, {"status": "yes"})],
        timer_minutes=20),
    "tutor_notes": {
        "name": "Tutor Session Notes", "type": "journal", "description": "notes from tutor sessions",
        "linked": ["language"],
        "fields": [
            {"id": "tutor", "name": "Tutor", "type": "text"},
            {"id": "focus", "name": "Focus", "type": "text"},
            {"id": "notes", "name": "Notes", "type": "multiline"},
        ],
        "entries": [
            (10, 18, {"tutor": "Ana", "focus": "past tenses",
                      "notes": "Good progress on the preterite. Next: ser versus estar"}),
            (4, 18, {"tutor": "Ana", "focus": "subjunctive",
                     "notes": "Weak spots are the subjunctive and ser versus estar. Flying to Lisbon "
                              "on 12 Dec, so we aim at a 10 minute conversation by then"}),
        ],
    },
    "vocab_log": {
        "name": "Vocabulary Log", "type": "tracker", "description": "new words",
        "linked": ["language"],
        "fields": [
            {"id": "word", "name": "Word", "type": "text", "required": True},
            {"id": "meaning", "name": "Meaning", "type": "text"},
            {"id": "mastered", "name": "Mastered", "type": "text"},
        ],
        "entries": [
            (6, 20, {"word": "saudade", "meaning": "longing", "mastered": "yes"}),
            (6, 20, {"word": "desculpe", "meaning": "sorry", "mastered": "yes"}),
            (4, 20, {"word": "embora", "meaning": "although", "mastered": "no"}),
            (2, 20, {"word": "contudo", "meaning": "however", "mastered": "no"}),
        ],
    },
}

TEAM_CONTEXT = {
    "name": f"{TAG} Team working agreement",
    "content": ("Working agreement for the test team: we never deploy or launch on a Friday. The "
                "weekly review is every Thursday at 4pm."),
    "pivot": "TEAM_FRIDAY_RULE",
}


# ---------------------------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------------------------

def category_lookup():
    status, body = api("GET", "/api/categories/all")
    cats = body.get("categories") if status == 200 and isinstance(body, dict) else []
    by_name = {}
    for c in cats:
        by_name.setdefault(c.get("name"), c.get("id"))
    return by_name


def build_collection(key, spec, quest_ids):
    linked = [quest_ids[q] for q in spec["linked"]]
    description = f"{spec['description']} ({TAG})"
    if spec["type"] == "habit":
        body = {"name": spec["name"], "description": description, "type": "habit",
                "habit_type": spec["habit_type"], "custom_fields": [], "linked_quest_ids": linked}
        if spec["habit_type"] == "timer":
            body["frequency"] = {"type": "daily", "durationGoalMinutes": spec["timer_minutes"]}
        else:
            body["frequency"] = {"type": "daily"}
            body["quick_entry_enabled"] = True
    else:
        body = {"name": spec["name"], "description": description, "type": spec["type"],
                "custom_fields": spec["fields"], "linked_quest_ids": linked}
    status, resp = api("POST", "/api/data/collections", body)
    assert status == 201, (key, status, resp)
    cid = resp["id"]
    for days, hour, values in spec["entries"]:
        if spec["type"] == "habit":
            # A habit entry is one row per period and the endpoint stamps it with TODAY unless the
            # period fields are explicit. (A timer habit's seconds cannot be seeded: the backend
            # derives them from session records, so a seeded timer row is completed with 0 s.)
            day = (TODAY - datetime.timedelta(days=days)).isoformat()
            values = {"entry_date": day, "period_start": day, "period_end": day, "period": "day",
                      "completed": True, "completionType": "yes"}
        st, er = api("POST", "/api/data/entries", {
            "collection_id": cid, "field_values": values, "created_at": ago(days, hour),
            "linked_quest_ids": linked})
        if st not in (200, 201):
            print(f"  WARN entry for {key} failed: {st} {str(er)[:200]}")
    return cid


ASKS_PATH = WORK_DIR / "world_asks.json"


def find_quest(spec):
    """An already-created world quest for this spec (matched by outcome + tag), else None."""
    for q in list_quests():
        state = q.get("state") or {}
        if state.get("outcome") == spec["outcome"] and TAG in str(
                state.get("acceptance_criteria") or ""):
            return q.get("quest_id")
    return None


def request_quest(spec, cats):
    """File the quest creation. An API-key caller is an AI actor, so Quest PARKS the request on an
    'Asks for you' decision (HTTP 202) until a PERSON approves it in a signed-in session; an API
    key can never approve it. Returns (quest_id or None, decision_id or None)."""
    category = next((cats[n] for n in spec["category_names"] if n in cats), FALLBACK_CATEGORY)
    status, body = api("POST", "/api/quests/start", {
        "category_id": category, "outcome": spec["outcome"],
        "acceptance_criteria": f"{spec['acceptance_criteria']} ({TAG})",
        "current_state": spec["current_state"],
        "timeline_days": spec["timeline_days"], "creation_mode": "quick"})
    if status == 201:
        return body["quest_id"], None
    if status == 202 and isinstance(body, dict):
        return None, body.get("decision_id")
    raise RuntimeError(f"quest creation failed: {status} {body}")


def ensure_quests(wait_seconds=0):
    """Make sure every world quest exists. Files the approval asks for missing ones, then polls up
    to ``wait_seconds`` for a person to approve them. Returns ({key: quest_id}, {key: decision_id})
    where the second dict is what is still pending."""
    import time
    cats = category_lookup()
    asks = json.loads(ASKS_PATH.read_text()) if ASKS_PATH.exists() else {}
    quests, pending = {}, {}
    deadline = time.time() + wait_seconds
    while True:
        quests, pending = {}, {}
        for key, spec in QUESTS.items():
            qid = find_quest(spec)
            if qid:
                quests[key] = qid
                asks.pop(key, None)
                continue
            if key not in asks:
                qid, decision = request_quest(spec, cats)
                if qid:
                    quests[key] = qid
                    continue
                asks[key] = decision
            pending[key] = asks[key]
        ASKS_PATH.write_text(json.dumps(asks, indent=1))
        if not pending or time.time() >= deadline:
            return quests, pending
        time.sleep(10)


def build_contents(state, quest_keys):
    """Goals, measurable outcomes, notes, quest docs, collections and team context."""
    out = state
    for key in quest_keys:
        spec, qid = QUESTS[key], out["quests"][key]
        api("POST", f"/api/teams/{QUEST_TEAM}/quest", {"quest_id": qid})  # lets Quest AI read goals
        api("PUT", f"/api/quests/{qid}/measurable-outcomes", {
            "outcomes": [{"text": t, "completed": False} for t in spec["measurable_outcomes"]]})
        out["goals"][key] = {}
        for name, period, scope, criteria in spec["goals"]:
            st, g = api("POST", "/api/planning/goals", {
                "quest_id": qid, "name": name, "period": period, "time_scope": scope,
                "criteria": criteria})
            assert st == 200, (key, name, st, g)
            out["goals"][key][name] = g["id"]
        for text, pivot in spec["notes"]:
            st, _ = api("POST", f"/api/quests/{qid}/notes", {"text": text})
            if st not in (200, 201):
                print(f"  WARN note failed {st}")
        for title, content, pivot in spec.get("docs", []):
            st, d = api("POST", f"/api/quests/{qid}/context-entries",
                        {"name": f"{TAG} {title}", "content": content})
            if st in (200, 201) and isinstance(d, dict) and d.get("id"):
                out["docs"][title] = {"quest": key, "id": d["id"]}
            else:
                print(f"  WARN quest doc failed {st} {str(d)[:200]}")
        STATE_PATH.write_text(json.dumps(out, indent=1))
        print(f"contents of quest {key} built")
    for key, spec in COLLECTIONS.items():
        if all(q in out["quests"] for q in spec["linked"]):
            out["collections"][key] = build_collection(key, spec, out["quests"])
            print(f"collection {key}: {out['collections'][key]}")
            STATE_PATH.write_text(json.dumps(out, indent=1))
    st, d = api("POST", f"/api/teams/{QUEST_TEAM}/context-entries",
                {"name": TEAM_CONTEXT["name"], "content": TEAM_CONTEXT["content"]})
    if st in (200, 201) and isinstance(d, dict) and d.get("id"):
        out["team_context"] = d["id"]
    else:
        print(f"  WARN team context failed {st} {str(d)[:200]}")
    out["contents_built"] = True
    STATE_PATH.write_text(json.dumps(out, indent=1))


def fresh_state():
    return {"quests": {}, "goals": {}, "collections": {}, "docs": {}, "team_context": None,
            "contents_built": False, "pivots": PIVOTS, "built": datetime.datetime.now().isoformat()}


def setup(wait_seconds=0):
    """Build the world. Needs one-time PERSON approval of the five quest creations (see
    request_quest). Exits 3 if any are still pending; re-run `setup` after approving."""
    if STATE_PATH.exists() and json.loads(STATE_PATH.read_text()).get("contents_built"):
        raise SystemExit(f"{STATE_PATH} exists: a world is already built. Run teardown or reset.")
    quests, pending = ensure_quests(wait_seconds)
    if pending:
        print("\nQUEST APPROVALS PENDING. A person must approve these on dev (the app's "
              "'Asks for you', signed in; an API key cannot approve):")
        for key, decision in pending.items():
            print(f"  {key}: {decision}   outcome: {QUESTS[key]['outcome']}")
        print(f"\nThen re-run setup. Asks are remembered in {ASKS_PATH}.")
        raise SystemExit(3)
    state = fresh_state()
    state["quests"] = quests
    state["cards_baseline"] = sorted(card_ids())
    STATE_PATH.write_text(json.dumps(state, indent=1))
    build_contents(state, list(QUESTS))
    print(f"world written to {STATE_PATH}")
    return state


def setup_partial():
    """SMOKE MODE ONLY: no quests (no approvals needed). Builds the collections that need no quest,
    unlinked, plus team context, so the runner/judge pipeline can be exercised end to end. A
    partial world cannot serve the datasets; it is labelled so in the state file."""
    if STATE_PATH.exists():
        raise SystemExit(f"{STATE_PATH} exists: run teardown first.")
    state = fresh_state()
    state["partial"] = True
    state["cards_baseline"] = sorted(card_ids())
    STATE_PATH.write_text(json.dumps(state, indent=1))
    for key in ("expense_log", "customer_interviews", "reading_log", "home_tasks"):
        spec = dict(COLLECTIONS[key])
        spec["linked"] = []
        state["collections"][key] = build_collection(key, spec, {})
        print(f"collection {key} (unlinked): {state['collections'][key]}")
        STATE_PATH.write_text(json.dumps(state, indent=1))
    state["contents_built"] = True
    STATE_PATH.write_text(json.dumps(state, indent=1))
    return state


def load():
    """The ids of the built world: {quests, goals, collections, docs, team_context, pivots}."""
    if not STATE_PATH.exists():
        raise SystemExit("no world built: run `runner.py setup` first")
    return json.loads(STATE_PATH.read_text())


# ---------------------------------------------------------------------------------------------
# Snapshot + diff: the side-effect detector the runner and the judge share.
# ---------------------------------------------------------------------------------------------

def entry_values(entry):
    values = entry.get("fieldValues") or entry.get("field_values") or {}
    return json.loads(json.dumps(values, default=str, sort_keys=True))


def all_tasks():
    status, body = api("GET", "/api/assistant-tasks", params={"limit": 200})
    tasks = body.get("tasks") if status == 200 and isinstance(body, dict) else []
    return [t for t in tasks if t.get("task_id") or t.get("id")]


def task_id_of(task):
    return task.get("task_id") or task.get("id")


def world_task_ids(world):
    """Tasks that belong to the eval: on a world quest, or carrying the tag. Tasks other people or
    lanes create on the shared dev account are NOT ours and are never diffed or deleted."""
    quest_ids = set(world["quests"].values())
    return {task_id_of(t): (t.get("text") or "")[:160] for t in all_tasks()
            if t.get("goal_id") in quest_ids or TAG in str(t.get("text") or "")}


def conversation_tasks(conv_id):
    """Tasks a chat turn queued (delegation), found by the conversation they came from."""
    return [t for t in all_tasks() if t.get("conv_id") == conv_id]


def snapshot(world=None):
    """Everything a chat turn could change, keyed so diff() can say precisely what moved."""
    world = world or load()
    snap = {"quests": {}, "collections": {}, "all_quest_ids": [], "all_collection_ids": [],
            "tasks": world_task_ids(world)}
    mine = {q.get("quest_id"): q for q in list_quests()}
    snap["all_quest_ids"] = sorted(mine)
    snap["all_collection_ids"] = sorted(c.get("id") for c in list_collections())
    for key, qid in world["quests"].items():
        state = (mine.get(qid) or {}).get("state") or quest_state(qid)
        goals = goals_of(qid)
        snap["quests"][key] = {
            "outcome": state.get("outcome"),
            "current_state": state.get("current_state"),
            "acceptance_criteria": state.get("acceptance_criteria"),
            "preferences": state.get("preferences") or None,
            "purpose": state.get("purpose") or None,
            "measurable_outcomes": [
                (m.get("text"), bool(m.get("completed"))) for m in
                (state.get("measurable_outcomes") or measurable_outcomes_of(qid))],
            "notes": {str(n.get("id")): str(n.get("text")) for n in notes_of(qid)},
            "goals": {
                (g.get("id") or g.get("goal_id")): {
                    "name": g.get("name") or g.get("title"), "period": g.get("period"),
                    "completed": bool(g.get("completed")),
                    "criteria": g.get("criteria")} for g in goals},
        }
    meta_by_id = {c.get("id"): c for c in list_collections()}
    snap["collection_meta"] = {}
    for key, cid in world["collections"].items():
        snap["collections"][key] = {
            e.get("id"): entry_values(e) for e in entries_of(cid) if e.get("id")}
        c = meta_by_id.get(cid) or {}
        snap["collection_meta"][key] = {
            "name": c.get("name"),
            "fields": sorted(str(f.get("id") or f.get("name")) for f in (c.get("custom_fields") or []))}
    return snap


def diff(before, after):
    """Human-readable list of changes between two snapshots (empty list = no side effects)."""
    changes = []
    for key, now in after["quests"].items():
        was = before["quests"].get(key, {})
        for field in ("outcome", "current_state", "acceptance_criteria", "preferences", "purpose"):
            if was.get(field) != now.get(field):
                changes.append(f"quest[{key}].{field}: {was.get(field)!r} -> {now.get(field)!r}")
        if was.get("measurable_outcomes") != now.get("measurable_outcomes"):
            changes.append(f"quest[{key}].measurable_outcomes: {was.get('measurable_outcomes')} "
                           f"-> {now.get('measurable_outcomes')}")
        was_notes = was.get("notes", {})
        for nid, text in now["notes"].items():
            if nid not in was_notes:
                changes.append(f"quest[{key}] NOTE ADDED: {text[:200]!r}")
            elif was_notes[nid] != text:
                changes.append(f"quest[{key}] NOTE EDITED: {was_notes[nid][:80]!r} -> {text[:200]!r}")
        for nid, text in was_notes.items():
            if nid not in now["notes"]:
                changes.append(f"quest[{key}] NOTE REMOVED: {text[:200]!r}")
        for gid, goal in now["goals"].items():
            old = was.get("goals", {}).get(gid)
            if old is None:
                changes.append(f"quest[{key}] GOAL ADDED: {goal}")
            elif old != goal:
                changes.append(f"quest[{key}] GOAL CHANGED {old.get('name')!r}: {old} -> {goal}")
        for gid, goal in was.get("goals", {}).items():
            if gid not in now["goals"]:
                changes.append(f"quest[{key}] GOAL REMOVED: {goal}")
    for key, now in after["collections"].items():
        was = before["collections"].get(key, {})
        for eid, values in now.items():
            if eid not in was:
                changes.append(f"collection[{key}] ENTRY ADDED: {values}")
            elif was[eid] != values:
                changes.append(f"collection[{key}] ENTRY CHANGED {eid}: {was[eid]} -> {values}")
        for eid, values in was.items():
            if eid not in now:
                changes.append(f"collection[{key}] ENTRY REMOVED: {values}")
    for key, now in after.get("collection_meta", {}).items():
        was = before.get("collection_meta", {}).get(key)
        if was is not None and was != now:
            changes.append(f"collection[{key}] SCHEMA CHANGED: {was} -> {now}")
    for qid in sorted(set(after["all_quest_ids"]) - set(before["all_quest_ids"])):
        changes.append(f"QUEST CREATED outside the world: {qid}")
    for qid in sorted(set(before["all_quest_ids"]) - set(after["all_quest_ids"])):
        changes.append(f"QUEST DELETED: {qid}")
    for cid in sorted(set(after["all_collection_ids"]) - set(before["all_collection_ids"])):
        changes.append(f"COLLECTION CREATED: {cid}")
    for cid in sorted(set(before["all_collection_ids"]) - set(after["all_collection_ids"])):
        changes.append(f"COLLECTION DELETED: {cid}")
    for tid, text in after["tasks"].items():
        if tid not in before["tasks"]:
            changes.append(f"TASK QUEUED {tid}: {text!r}")
    return changes


def card_ids():
    """Ids of the account's (non-managed) context cards. Chat turns LEARN cards (topic cards and a
    card per conversation) that persist across conversations and would leak one case into the
    next, so the runner and teardown delete the ones created since the baseline."""
    status, body = api("GET", "/api/cards")
    cards = body.get("cards") if status == 200 and isinstance(body, dict) else []
    return {c.get("id") for c in cards if c.get("id")}


def delete_new_cards(baseline):
    """Delete cards that did not exist at setup. Returns how many were removed."""
    if baseline is None:
        return 0  # no baseline recorded: refuse to guess which cards are ours
    fresh = card_ids() - set(baseline)
    for cid in fresh:
        api("DELETE", f"/api/cards/{cid}")
    return len(fresh)


def ground_truth(quest_keys=None):
    """Plain-text description of what was SEEDED, for the judge to verify facts against."""
    keys = quest_keys or list(QUESTS)
    lines = [f"Today is {TODAY.isoformat()}."]
    for key in keys:
        q = QUESTS[key]
        lines.append(f"\n== QUEST '{key}' ==")
        lines.append(f"Outcome: {q['outcome']}")
        lines.append(f"Acceptance criteria: {q['acceptance_criteria']}")
        lines.append(f"Current state: {q['current_state']}")
        lines.append("Measurable outcomes: " + "; ".join(q["measurable_outcomes"]))
        lines.append("Goals: " + "; ".join(f"{g[0]} ({g[1]})" for g in q["goals"]))
        for text, _ in q["notes"]:
            lines.append(f"Quest note: {text}")
        for title, content, _ in q.get("docs", []):
            lines.append(f"Quest doc '{title}': {content}")
        for ckey, c in COLLECTIONS.items():
            if key not in c["linked"]:
                continue
            lines.append(f"Collection '{c['name']}' ({ckey}, {c['type']}"
                         f"{', ' + c['habit_type'] if c.get('habit_type') else ''}):")
            for days, _hour, values in c["entries"]:
                day = (TODAY - datetime.timedelta(days=days)).isoformat()
                shown = "completed" if c["type"] == "habit" else json.dumps(values)
                lines.append(f"  - {day}: {shown}")
    lines.append(f"\nTeam context: {TEAM_CONTEXT['content']}")
    return "\n".join(lines)


def revert(before, after, world=None):
    """Best-effort undo of a case's side effects so the next case starts from the seeded world.
    Returns True when a fresh snapshot equals ``before`` again; otherwise the caller should
    ``reset()`` (which needs no new approvals)."""
    world = world or load()
    for tid in set(after["tasks"]) - set(before["tasks"]):
        api("DELETE", f"/api/assistant-tasks/{tid}")
    for key, now in after["quests"].items():
        was = before["quests"].get(key, {})
        qid = world["quests"][key]
        for field in ("outcome", "current_state", "acceptance_criteria"):
            if was.get(field) != now.get(field) and was.get(field) is not None:
                api("PATCH", f"/api/quests/{qid}/field", {"field_name": field, "value": was[field]})
        # preferences and purpose are seeded empty: a case that set one is cleared (best effort).
        for field in ("preferences", "purpose"):
            if was.get(field) is None and now.get(field) is not None:
                api("PATCH", f"/api/quests/{qid}/field", {"field_name": field, "value": ""})
        for nid in set(now["notes"]) - set(was.get("notes", {})):
            api("DELETE", f"/api/quests/{qid}/notes/{nid}")
        for gid, goal in now["goals"].items():
            old = was.get("goals", {}).get(gid)
            if old is None:
                api("DELETE", f"/api/planning/goals/{gid}")
            elif old != goal:
                api("PUT", f"/api/planning/goals/{gid}", {
                    "name": old.get("name"), "completed": old.get("completed"),
                    "criteria": old.get("criteria")})
    for key, now in after["collections"].items():
        was = before["collections"].get(key, {})
        cid = world["collections"][key]
        for eid, values in now.items():
            if eid not in was:
                api("DELETE", f"/api/data/entries/{eid}", params={"collection_id": cid})
            elif was[eid] != values:
                api("PUT", f"/api/data/entries/{eid}", {"field_values": was[eid]},
                    params={"collection_id": cid})
    # collections the chat created (an explicit "create a collection" request) are deleted
    for cid in set(after["all_collection_ids"]) - set(before["all_collection_ids"]):
        api("DELETE", f"/api/data/collections/{cid}")
    again = snapshot(world)
    return not diff(before, again)


# ---------------------------------------------------------------------------------------------
# Teardown / reset
# ---------------------------------------------------------------------------------------------

def restore_quest_fields(key, qid):
    """Put the editable quest fields back to their seeded values (human edit route)."""
    spec = QUESTS[key]
    for field, value in (("outcome", spec["outcome"]),
                         ("current_state", spec["current_state"]),
                         ("acceptance_criteria", f"{spec['acceptance_criteria']} ({TAG})")):
        api("PATCH", f"/api/quests/{qid}/field", {"field_name": field, "value": value})


def teardown(keep_quests=False, decline_asks=False):
    """Delete the world and PROVE it is gone. ``keep_quests`` leaves the five quests (whose
    creation needed a person's approval) in place with their fields restored, so a re-setup needs
    no new approvals; everything else is deleted and verified either way."""
    state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else fresh_state()
    report = []

    # Sweep by TAG too, so a half-built or orphaned world is still cleaned.
    tagged_quests = set(state["quests"].values())
    for q in list_quests():
        s = q.get("state") or {}
        if TAG in str(s.get("acceptance_criteria") or ""):
            tagged_quests.add(q.get("quest_id"))
    tagged_collections = set(state["collections"].values())
    for c in list_collections():
        if TAG in str(c.get("description") or ""):
            tagged_collections.add(c.get("id"))

    # Any assistant task on a world quest (e.g. a delegated turn) goes too, so the dev runner lane
    # never executes work queued by an eval turn.
    for t in all_tasks():
        if t.get("goal_id") in tagged_quests or TAG in str(t.get("text") or ""):
            tid = task_id_of(t)
            report.append(("task", tid, api("DELETE", f"/api/assistant-tasks/{tid}")[0]))

    for qid in tagged_quests:
        for goal in goals_of(qid):
            gid = goal.get("id") or goal.get("goal_id")
            report.append(("goal", gid, api("DELETE", f"/api/planning/goals/{gid}")[0]))
        if keep_quests:
            for note in notes_of(qid):
                if note.get("id"):
                    report.append(("note", note["id"], api(
                        "DELETE", f"/api/quests/{qid}/notes/{note['id']}")[0]))
    for cid in tagged_collections:
        report.append(("collection", cid, api("DELETE", f"/api/data/collections/{cid}")[0]))
    for title, ref in state.get("docs", {}).items():
        qid = state["quests"].get(ref["quest"])
        if qid:
            report.append(("quest doc", ref["id"],
                           api("DELETE", f"/api/quests/{qid}/context-entries/{ref['id']}")[0]))
    if state.get("team_context"):
        report.append(("team context", state["team_context"], api(
            "DELETE", f"/api/teams/{QUEST_TEAM}/context-entries/{state['team_context']}")[0]))
    if keep_quests:
        for key, qid in state["quests"].items():
            restore_quest_fields(key, qid)
    else:
        for qid in tagged_quests:
            report.append(("quest", qid, api("DELETE", f"/api/quests/{qid}")[0]))
    if decline_asks and ASKS_PATH.exists():
        for key, decision in json.loads(ASKS_PATH.read_text()).items():
            report.append(("ask", decision, api(
                "POST", f"/api/teams/decisions/{decision}/resolve", {"resolution": "decline"})[0]))
        ASKS_PATH.unlink()
    removed = delete_new_cards(state.get("cards_baseline"))
    if state.get("cards_baseline") is not None:
        print(f"deleted {removed} context card(s) learned during eval turns")
    for kind, ident, status in report:
        print(f"delete {kind} {ident}: HTTP {status}")

    print("\n--- verifying it is really gone ---")
    gone = True
    mine = {q.get("quest_id") for q in list_quests()}
    if keep_quests:
        print(f"quests kept on purpose: {len(tagged_quests & mine)}/{len(tagged_quests)}")
        for key, qid in state["quests"].items():
            now = quest_state(qid)
            for field, want in (("outcome", QUESTS[key]["outcome"]),
                                ("current_state", QUESTS[key]["current_state"])):
                if now.get(field) != want:
                    print(f"quest {key}.{field} NOT restored: {now.get(field)!r}")
                    gone = False
            leftover = notes_of(qid)
            if leftover:
                print(f"quest {key} still has {len(leftover)} note(s)")
                gone = False
    else:
        still = sorted(tagged_quests & mine)
        print(f"quests still on the account: {len(still)} {still}")
        gone &= not still
        status, body = api("GET", f"/api/teams/{QUEST_TEAM}/quests")
        board = {q.get("quest_id") for q in body} if isinstance(body, list) else set()
        print(f"quests still on the team board: {sorted(tagged_quests & board)}")
        gone &= not (tagged_quests & board)
    for cid in sorted(tagged_collections):
        status, _ = api("GET", f"/api/data/collections/{cid}")
        if status not in (404, 403):
            print(f"collection {cid} -> {status} (STILL THERE)")
            gone = False
    left = [c.get("name") for c in list_collections() if TAG in str(c.get("description") or "")]
    print(f"{TAG} collections still on the account: {len(left)} {left}")
    gone &= not left
    for qid in tagged_quests:
        remaining = goals_of(qid)
        if remaining:
            print(f"goals still on {qid}: {[g.get('name') for g in remaining]}")
            gone = False
    if state.get("team_context"):
        status, body = api("GET", f"/api/teams/{QUEST_TEAM}/context-entries")
        ids = {e.get("id") for e in unwrap_list(body, "entries", "items")}
        print(f"team context entry still present: {state['team_context'] in ids}")
        gone &= state["team_context"] not in ids
    pending = json.loads(ASKS_PATH.read_text()) if ASKS_PATH.exists() else {}
    if pending:
        print(f"note: {len(pending)} quest-creation ask(s) remain open for a person: {pending}")
    print("\nCLEANUP VERIFIED" if gone else "\nCLEANUP INCOMPLETE")
    if gone and STATE_PATH.exists():
        STATE_PATH.unlink()
        if keep_quests and not state.get("partial"):
            kept = fresh_state()
            kept["quests"] = state["quests"]
            kept["cards_baseline"] = state.get("cards_baseline")
            STATE_PATH.write_text(json.dumps(kept, indent=1))
    return gone


def reset():
    """Return the world to its seeded state without new approvals: teardown(keep_quests) + rebuild."""
    state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else None
    if state is None or state.get("partial") or not state.get("quests"):
        raise SystemExit("reset needs a full world; run setup")
    if not teardown(keep_quests=True):
        raise SystemExit("reset aborted: cleanup incomplete")
    state = json.loads(STATE_PATH.read_text())
    build_contents(state, list(QUESTS))


def show():
    world = load()
    print(json.dumps({k: v for k, v in world.items() if k != "pivots"}, indent=1))
    for name, p in PIVOTS.items():
        print(f"{name} [{p['quest_key']}]: {p['description']}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cmd", choices=["setup", "teardown", "reset", "show"])
    parser.add_argument("--wait", type=int, default=0,
                        help="setup: seconds to poll for a person to approve the quest asks")
    parser.add_argument("--partial", action="store_true", help="setup: smoke world, no quests")
    parser.add_argument("--keep-quests", action="store_true", help="teardown: keep approved quests")
    parser.add_argument("--decline-asks", action="store_true",
                        help="teardown: decline any still-open quest-creation asks")
    args = parser.parse_args()
    if args.cmd == "setup":
        setup_partial() if args.partial else setup(args.wait)
    elif args.cmd == "teardown":
        ok = teardown(args.keep_quests, args.decline_asks)
        sys.exit(0 if ok else 1)
    elif args.cmd == "reset":
        reset()
    else:
        show()
