"""The data of the disposable chat-quality world: date helpers, the named PIVOT facts, the five quests with their goals and notes, the collections with seeded entries, the autopilot mode per quest and the team working agreement. Pure data, no network; world.py builds, snapshots and tears down what this describes and re-exports every name here."""
import datetime

from devclient import TAG

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

#: Which quests have opted into letting the AI apply a field change it was asked for, and which
#: have not. ``family`` stays ``off`` on purpose: it is the only quest where the correct behaviour
#: for "set my preferences to X" is to PROPOSE the change and say it was not applied, so the suite
#: tests both halves of quest-backend's ``check_ai_field_write`` gate rather than only the happy one.
AUTOPILOT_BY_QUEST = {"fitness": "act", "business": "act", "research": "act", "language": "act",
                      "family": "off"}


TEAM_CONTEXT = {
    "name": f"{TAG} Team working agreement",
    "content": ("Working agreement for the test team: we never deploy or launch on a Friday. The "
                "weekly review is every Thursday at 4pm."),
    "pivot": "TEAM_FRIDAY_RULE",
}
