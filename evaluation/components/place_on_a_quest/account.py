"""The synthetic account every case of ``place_on_a_quest`` is placed against: ten active quests with distinct themes and two
look-alike pairs (the app launch and the website redesign share "launch"; the grant and the newsletter share "funding"/"growth"
vocabulary; the bookkeeper hire and the team offsite are both Operations). Invented for this metric: no Spiritual Data name, no
private text. A quest has the fields Quest's ranker reads: its outcome (title), category, current state, the owner's scope and the
open measurable outcomes (the milestones)."""

CORE = [
    {"quest_id": "q_app", "outcome": "Ship the mobile app to the app stores", "category": "Product",
     "state": "Build 14 passes testing. Store listing screenshots are drafted. Waiting on the review queue.",
     "scope": "", "open": ["Submit the iOS build to App Store review", "Publish the Android build on Google Play",
                            "Collect feedback from 20 beta testers"]},
    {"quest_id": "q_site", "outcome": "Redesign the company website", "category": "Product",
     "state": "New homepage mockups are approved. Copy for the pricing page is pending.",
     "scope": "Website work only, not the mobile app.", "open": ["Launch the new homepage", "Rewrite the pricing page copy",
                                                                  "Redirect the old URLs"]},
    {"quest_id": "q_run", "outcome": "Run a half marathon in under two hours", "category": "Health",
     "state": "Weekly long run is at 16 km. The knee feels fine.",
     "scope": "", "open": ["Complete an 18 km long run", "Follow the race-week nutrition plan", "Buy new running shoes"]},
    {"quest_id": "q_paper", "outcome": "Publish a paper on sleep and memory", "category": "Research",
     "state": "The methods section is drafted. Data analysis is finished.",
     "scope": "", "open": ["Write the discussion section", "Submit the manuscript to the journal", "Respond to co-author comments"]},
    {"quest_id": "q_grant", "outcome": "Win a foundation grant for the community garden", "category": "Funding",
     "state": "The letter of inquiry was accepted. The full proposal is due next month.",
     "scope": "", "open": ["Finish the budget narrative", "Get letters of support from two partners", "Schedule the funder call"]},
    {"quest_id": "q_kitchen", "outcome": "Renovate the kitchen", "category": "Home",
     "state": "The cabinet design is chosen. Contractor quotes are arriving.",
     "scope": "", "open": ["Compare three contractor quotes", "Order the cabinets", "Apply for the building permit"]},
    {"quest_id": "q_news", "outcome": "Grow the newsletter to 1,000 subscribers", "category": "Growth",
     "state": "Now at 640 subscribers. The welcome email is live.",
     "scope": "", "open": ["Add a referral program", "Write the next four weekly issues", "Test a new signup form on the blog"]},
    {"quest_id": "q_hire", "outcome": "Hire a part-time bookkeeper", "category": "Operations",
     "state": "The job post is written. Twelve people applied.",
     "scope": "Use for hiring and payroll for the bookkeeper. Do not use for team events.",
     "open": ["Interview the top four applicants", "Check references for the finalist", "Set up payroll access"]},
    {"quest_id": "q_spanish", "outcome": "Learn conversational Spanish", "category": "Learning",
     "state": "Finished unit 6. Tutor sessions are weekly on Thursday.",
     "scope": "", "open": ["Hold a ten minute conversation with the tutor", "Learn the 500 most common verbs",
                            "Watch a film without subtitles"]},
    {"quest_id": "q_offsite", "outcome": "Plan the team offsite in June", "category": "Operations",
     "state": "A shortlist of three venues. The budget is approved.",
     "scope": "", "open": ["Book the venue", "Send the agenda to the team", "Arrange travel for remote staff"]},
]


def quest_label(quest):
    """The title line a ranker shows: category and outcome."""
    return f"{quest['category']} · {quest['outcome']}"


def embedding_text(quest):
    """What the shared ranker's embedding covers: the outcome, the category and the scope (quest_selection.semantic_hits)."""
    return " ".join(p for p in (quest["outcome"], quest["category"], quest["scope"]) if p)


def card_text(quest):
    """What the hybrid ranker's quest card covers: the outcome, state and open outcomes (a longer digest than the outcome alone)."""
    return " ".join([quest["outcome"], quest["category"], quest["state"], *quest["open"]])


# Thirty more quests of a busy account, fixed before any arm was scored on them, so the candidate window of a model (the first 25 it is shown)
# no longer holds everything: the order the candidates arrive in matters, which is what a candidate generator is for. Their themes differ from
# the ten above, with a few near neighbours (a second fitness quest, a second writing quest, a second home quest).
FILLERS = [
    ("Save six months of expenses", "Finance", "Savings stand at four months.", ["Open a high yield savings account", "Automate a monthly transfer"]),
    ("Learn to play the guitar", "Hobbies", "Can play five open chords.", ["Learn the barre chord", "Play one full song at tempo"]),
    ("Write a children's book", "Writing", "Outline is done and two chapters drafted.", ["Draft chapter three", "Find an illustrator"]),
    ("Start a vegetable garden at home", "Home", "Raised beds are built.", ["Buy seedlings", "Install the watering timer"]),
    ("Get the car serviced and inspected", "Home", "The inspection sticker expires next month.", ["Book the garage", "Replace the wiper blades"]),
    ("Strength training three times a week", "Health", "Two sessions a week so far.", ["Add a third gym day", "Track the deadlift weight"]),
    ("Meditate every morning", "Health", "A streak of nine days.", ["Reach a thirty day streak", "Try a longer session"]),
    ("Plan the family reunion", "Family", "A date is proposed for August.", ["Collect headcount", "Reserve the park pavilion"]),
    ("Visit grandparents more often", "Family", "Last visit was in March.", ["Schedule a weekend trip", "Set up a weekly video call"]),
    ("Organize the photo archive", "Home", "Ten thousand photos are unsorted.", ["Remove duplicates", "Back up to cloud storage"]),
    ("Complete the data science certificate", "Learning", "Module four of eight.", ["Finish the statistics module", "Submit the capstone project"]),
    ("Prepare for the board exam", "Learning", "Study plan covers the first half.", ["Take a practice test", "Review the weak chapters"]),
    ("Launch an online course", "Business", "The first lessons are recorded.", ["Record the remaining lessons", "Set up the payment page"]),
    ("Close the quarterly books", "Business", "Receipts are half entered.", ["Reconcile the bank accounts", "File the sales tax return"]),
    ("Negotiate the vendor contract renewal", "Business", "The current contract ends in December.", ["Request a competing quote", "Schedule the negotiation call"]),
    ("Build a customer feedback survey", "Business", "Questions drafted.", ["Pilot the survey with ten customers", "Analyze the results"]),
    ("Reduce the cloud hosting bill", "Technology", "The bill doubled last quarter.", ["Shut down idle servers", "Move logs to cold storage"]),
    ("Migrate the database to the new version", "Technology", "A staging copy is running.", ["Run the migration rehearsal", "Plan the downtime window"]),
    ("Write the onboarding guide for new engineers", "Technology", "Outline agreed.", ["Document the local setup", "Record a walkthrough video"]),
    ("Adopt a rescue dog", "Family", "Visited two shelters.", ["Fill in the adoption form", "Buy a crate and bed"]),
    ("Plan a trip to Japan", "Travel", "Flights are tracked.", ["Book the flights", "Reserve the rail pass", "Draft the itinerary"]),
    ("Hike the coast trail in summer", "Travel", "Route chosen, permits needed.", ["Reserve the campsites", "Test the new tent"]),
    ("Volunteer at the food bank", "Community", "Signed up for orientation.", ["Attend the orientation", "Commit to one shift a month"]),
    ("Organize the neighborhood cleanup day", "Community", "Neighbors are interested.", ["Get trash bags donated", "Post the flyer"]),
    ("Give a talk at the local conference", "Speaking", "Proposal submitted.", ["Write the slides", "Rehearse twice"]),
    ("Run a monthly book club", "Community", "Six members.", ["Pick next month's book", "Send the invitation"]),
    ("Paint the living room", "Home", "Colors are chosen.", ["Buy paint and tape", "Move the furniture"]),
    ("Quit sugary drinks", "Health", "Down to two a week.", ["Stock sparkling water", "Go thirty days with none"]),
    ("Renew the passport and driver license", "Admin", "Passport expires in four months.", ["Book the passport appointment", "Take the new photo"]),
    ("Declutter the garage", "Home", "Half the boxes are labeled.", ["Donate the old tools", "Install wall shelves"]),
]

QUESTS = list(CORE) + [
    {"quest_id": f"q_filler_{n:02d}", "outcome": outcome, "category": category, "state": state, "scope": "", "open": open_items}
    for n, (outcome, category, state, open_items) in enumerate(FILLERS, start=1)]
# A fixed shuffle (a stride that visits every position once), so the quests the cases are about are spread through the account's order.
QUESTS = [QUESTS[(n * 7) % len(QUESTS)] for n in range(len(QUESTS))]


def quests_of(case):
    """The quests a case is placed against: its own (a private labelled case carries the quests it was labelled against), else the account's."""
    return case.get("quests") or QUESTS
