"""Coarse general-English word-frequency baseline.

This module supplies a coarse baseline for asking "is this word common in
general English, or is it distinctive?" It is deliberately low resolution:
words are sorted into three bands of descending commonness rather than
ranked by exact frequency, because for this purpose only the band matters,
never an exact rank.

BAND_COMMON holds roughly the most frequent words in English: function
words, pronouns, auxiliaries, prepositions, conjunctions, determiners, and
a handful of ultra-common verbs with their everyday inflections.

BAND_FREQUENT holds a wider set of very common everyday words: ordinary
nouns, verbs, adjectives, adverbs, numbers, time words, and the basic
vocabulary of everyday life.

BAND_ORDINARY holds a broad set of further ordinary words that an average
adult uses in everyday writing and personal journaling without those words
being distinctive: goals, habits, feelings, work, money, health, home,
relationships, and the like.

A word that is absent from every band is treated by the caller as rare or
distinctive, since this baseline only records what is common, not what is
uncommon.

This is general English only. It deliberately carries no product, company,
or domain vocabulary: no proper nouns, no brand or technical terms, no
spiritual or niche jargon, and no names of people or places. Callers rely
on that absence to detect distinctive vocabulary against this baseline.
"""

from __future__ import annotations


BAND_COMMON = frozenset(
    """
    a about above after again against all am an and any are as ask asked asking asks at be because
    been before being below between both but by call called calling calls came can come comes
    coming could did do does doing done down during each feel feeling feels felt few find finding
    finds for found from further gave get gets getting give given gives giving go goes going gone
    got gotten had has have having he her here hers herself him himself his how i if in into is it
    its itself knew know knowing known knows leave leaves leaving left look looked looking looks
    made make makes making may me might mine more most must my myself no nor not of off on once
    only or other our ours ourselves out over own said same saw say saying says see seeing seem
    seemed seeming seems seen sees shall she should so some such take taken takes taking tell
    telling tells than that the their theirs them themselves then there these they think thinking
    thinks this those thought through to told too took tried tries try trying under up us use used
    uses using very want wanted wanting wants was we went were what when where which who whom whose
    why will with without work worked working works would you your yours yourself yourselves
    """.split()
)


BAND_FREQUENT = frozenset(
    """
    actually afternoon age air airport allow almost already also always angry animal answer area
    arm arms army baby back bad ball bathroom battle beach beaches beat beautiful bedroom begin
    believe big bike bird black blue boat body book boss boy bread breakfast bridge bring brother
    brown building bus business buy camera cameras car carry cat certain certainly change cheap
    chest child children choice church citizen citizens city class clean client close closed cloud
    coast coffee cold color company continue cool cost country court cried crime criminal crowd cry
    crying culture curly customer dance dark daughter day decision deep defeat describe desert
    dinner dirty dog dollar door draw drawing drew drink drive driving drove dry dust ear early
    earth easy eat eight eighth election eleven employee empty end engine engines enough envelope
    even evening ever every expensive explain eye eyes face faith fall false family fast fat father
    feet fifth fifty film films fine finger fire first fish five flew floor flower fly flying folks
    food foot forest forests forget form forty four fourth free friend fruit full future game games
    garden girl glad glass good government grass gray great green group grow guy hair half hand
    hands handsome happy hard head hear heart heavy help high hill hills history hold home hope
    hospital hot house hundred husband idea international island job journey judge jury just keep
    kid kind king kitchen lake large last late later laugh laughed laughing law laws learn leg legs
    let letter letters light listen little local long lose loud low lunch machine machines man many
    market match matches maybe meal meat meeting metal milk million mind minister moment money
    month moon morning mother mountain mountains mouth move movie movies much music name narrow
    national nature need never new news next nice night nine ninth nose now number ocean office
    officer often old one open orange package packages page paint painted painting paper parent
    parents park part passenger past path pay people perhaps person phone photo photos picture
    pictures pink place plan plane plastic play point police poor pray prayed prayer praying
    present president pretty price prince princess prison private probably problem public pull
    purple push put quarter queen question quiet quite radio rain rarely rather read real really
    reason receive red religion religious remember result rich ride riding right rise river rivers
    road rock rocks rode roof room round run sad sail sailing salt sand save school screen screens
    sea second sell send sentence seven seventh several shallow shape ship ships shop short shout
    shouted shouting side sing single sister sit six sixth size skin sky slow small smile smiled
    smiling snow society soft soldier sometimes son song songs soon sort speak spend sport sports
    square stamp stand star start state station still stone stop store storm story straight stream
    street strong study style sugar sun talk tall tea teach team television temple ten tenth thick
    thin third thirty thousand three ticket tickets time today tomorrow tool tools town train
    travel tree trip true turn twelve twenty two type ugly understand usually valley valleys
    various vegetable victory video videos vote voting walk wall war warm water weak week wet wheel
    wheels whisper whispered whispering white whole wide wife win wind window wish woman wood word
    world write wrong yard year yellow yesterday yet young
    """.split()
)


BAND_ORDINARY = frozenset(
    """
    abilities ability accomplish accomplished accomplishing account accounts achieve achieved
    achievement achieving acupuncture adapt adapted adapting adjust adjusted adjusting advantage
    advantages adventure adventures advice agenda airplane alarm alarms alcohol alert alerts
    allergies allergy allowance alternative alternatives ambition ambitious analytics anger
    anniversary annoyed annual answers anticipate anticipated anticipating anxiety anxious
    apologize apologized apology appetite apple apples applicant application applications
    appointment appointments appraisal appraised appreciate appreciated appreciating appreciation
    appreciative apprenticeship approach approaches apps archive archived archiving argue argued
    arguing argument arrange arranged arranging article articles ashamed assess assessed assessing
    assignment assignments assume assumed assuming attachment attachments attitude attitudes
    audience autumn avoid avoided avoiding awkward babies babyproofing babysitter backlog backup
    backups bake baked bakery baking balance balanced banana bananas bank banking batteries battery
    bed bedtime beef beer belief beliefs benefit benefits beverage beverages bicycle bill bills
    bins birthday blanket blankets blend blended blending blinds blog bloodpressure bloodwork
    bluetooth boil boiled boiling bonus bonuses brainstorm brainstormed brainstorming brakes break
    breaks breastfeeding breathe breathed breathing breeze brightness broom budget budgeting
    budgets bug bugs burned burnout butter cable cables cafe cake calculate calculated calculating
    calendar calendars calm calmness calorie calories candidate carbs card cardio cards cared
    career caring carpet carpool cash cats celebrate celebrating celebration centered chair chairs
    challenge challenges challenging chapter chapters charger charity chart charts chatbot
    checklist checkoff checkup checkups cheese chicken childproofing chiropractor chocolate
    cholesterol choose chooses choosing chop chopped chopping chore chores clarified clarify
    clarifying cleaned cleaning clicks clients climate clinic clock closet clothing cloudy coach
    coaching coat coats coding collaborate collaborating collaboration colleague colleagues college
    combine combined combining comfort comfortable commitment committed communicate communicated
    communicating communication commute commuting compassion compassionate complaint complaints
    complete completed completing complicate complicated complicating compost compress compressed
    computer computers conclude concluded concluding conference confidence confident confirm
    confirmed confirming conflict conflicts congestion connected connection consider considered
    considering consistency consistent content contentment contract contractor contracts
    conversation conversations conversions convert converted converting cook cooked cookie cookies
    cooking cooling couch counseling counselor course courses coworker coworkers craving cravings
    creative creativity credit crib cupboard curfew curiosity curious curriculum cursor curtains
    cyclist daily dashboard data database databases daycare deadline deadlines debt debts decide
    decides deciding decisions declutter decluttered decluttering dedicated dedication degree
    degrees deliverable deliverables deliveries delivery dental dentist deposit depression designed
    designing desire desired desiring desktop dessert desserts destination destinations detail
    details determine determined determining develop developed developer developers developing
    device devices diagnosed diagnosis diaper diapers diary dice diced dicing diet dieting dine
    dined dining disadvantage disadvantages disagree disagreed disagreement discipline disciplined
    disgust disgusted dishes dishwasher dislike disliked dissatisfied divide divided dividing
    doctor document documents dogs donate donated donating donation doubt doubtful downgrade
    downgraded downgrading draft drafted drafting drawer drawers dream dreamed dreaming dress
    dresses dropout drought edit edited editing education educational effective effectiveness
    efficiency efficient egg eggs electricity email emails embarrassed embarrassment emotion
    emotional emotions empathetic empathy employees employer encourage encouraged encouraging
    energetic energy engagement engineer engineers enjoy enjoyed enjoying enrolled enrollment
    ensure ensured ensuring entertainment entries entry envious envy equity ergonomics estimate
    estimated estimating estimator evaluate evaluated evaluating event events eviction exam exams
    exchange excited exercise exercised exercising exhausted exhaustion expand expanded expanding
    expect expected expecting expense expenses explore explored exploring export exported exporting
    fact facts fail failed failing failure fatigue fatigued fear fearful feature features feedback
    feelings fellowship ferry file files filing filter filters fitness fix fixed fixing flexibility
    flight flights flood flooring focus focused focusing fog folder folders forecast forecasted
    forgave forgive forgiveness formula formulas freelance freelancing fridge fried friends
    frustrated frustration fry frying fuel fundraiser fundraising furniture garage garbage gas
    gasoline gathering generosity generous gift gifts gloves goal goals grade grades graduate
    graduated graduation grant grants graph graphs grateful gratitude grill grilled grilling
    groceries grocery grooming grounded guest guests guidance guilt guilty gym habit habits hail
    haircut hardware hat hate hated hats health healthy heartbeat heating helpdesk hiring hobbies
    hobby holiday holidays homework honest honesty hopeful hopeless host hosted hosting hotel
    hotels humidity hydrate hydrated hydrating hydration ideas identified identify identifying
    illness imagination imaginative imagine imagined imagining impatient import important imported
    importing impressions improve improved improvement improving inbox income incomplete
    inconsistent increase increased increasing information ingredient ingredients insomnia
    inspiration inspire inspired inspiring install installed installing insulation insurance
    internet internship interview interviews inventory invest investment investments invitation
    invitations invite invited invoice invoices irritated isolated jacket jackets jealous jealousy
    jog jogging journal journaled journaling joy joyful juice keyboard kids kindness knowledge
    label labeling labels lamp lamps landfill landlord laptop launch launched launching laundry
    layoff layoffs lead leader leaders leadership leading learned learning learns lease leasing
    lecture lectures leftovers leisure lesson lessons lighting lightning like liked liking limit
    limited limiting livechat loan loans login logout loneliness lonely love loved loving maintain
    maintained maintaining maintenance manager managers manicure marinate marinated marinating
    massage meals measure measured measuring mechanic media medication medicine meditate meditated
    meditating meditation meetings membership memberships mentor mentoring mentorship message
    messages metabolism method methods metrics microwave mileage milestone milestones mindful
    mindfully mindfulness mindset minutes mirror mission mist misunderstanding mobility modem
    modified modify modifying monitor monitored monitoring monthly mood moods mop mortgage
    motivated motivating motivation motorcycle mouse nanny nap napping necessary negotiate
    negotiated negotiating negotiation neighbor neighborhood neighbors nervous nervousness network
    networks newsletter nightly note notes notice noticed noticing notification notifications
    nutrition nutritious objective objectives observe observed observing odometer oil onboarding
    online opportunities opportunity option optional options oranges organization organize
    organized organizer organizing orientation outcome outcomes outline outlook output oven overdue
    overtime overwhelm overwhelmed overworked paintjob pants paperwork parenting parking parties
    partner party password passwords pasta patience patient pause paused payment payments peace
    peaceful pedestrian pedicure pending pension permit permitted permitting perspective
    perspectives pet pets pharmacy phones physiotherapy pick picked picking pillow pillows pitch
    pitched pitching pivot pizza planned planner planning plans platform platforms playdate podcast
    pork portion portions posts posture practice practiced practices practicing predict predicted
    predicting prefer preferences preferred preferring prepare prepared preparing prescription
    prescriptions presentation presentations prevent prevented preventing pride printer priorities
    priority probation process processes productive productivity professor profile program
    programming progress project projects promotion promotions proposal proposals protein proud
    publish published publishing pulse purchase purchased purchasing quarterly questions quotation
    quote rainy ran reading reads receipt receipts recipe recipes recognize recognized recognizing
    reconcile reconciled reconciliation recover recovering recovery recruiter recruitment recycle
    recycling reduce reduced reducing refinance refinanced refinancing reflect reflected reflecting
    reflection refrigerator refund refunds registered registration relationship relationships relax
    relaxed relaxing release released releasing relief relieved reminder reminders remodel
    remodeled renew renewable renewal renewed renovated renovating renovation rent repair repaired
    repairing replied reply replying report reports required research researched researching
    resignation resolution resolve resolved resolving respect respected respecting respond
    responded responding rest restaurant rested resting restrict restricted restricting resume
    retirement returns reuse reused review reviewed reviewing revise revised revising reward
    rewards rice risk risks roadmap roast roasted roasting rollout rooms router routine routines
    rubbish running salad salary sandwich sandwiches satisfaction satisfied saving savings scan
    scanner scarf scarves schedule scheduled scheduling scholarship scholarships schools scooter
    season seasoning seasons select selected selecting selfcare separate separated separating
    server servers serving servings session sessions settings severance shame shampoo shave shaved
    shaving shelf shelves shift shifts shipment shipments shirt shirts shoes shopping shrink
    shrinking sick sickness signin signup simplified simplify simplifying sink skill skills
    skincare skirt skirts sleep sleeping slept slice sliced slicing snack snacks soap social socks
    soda sofa software solar solution solutions sorrow sorrowful sorted sorting soup specified
    specify specifying spending spent spice spices sponsor sponsored sponsoring sponsorship
    spreadsheet spreadsheets spring sprint sprints stakeholder stakeholders standup status steam
    steamed steaming steps stipend stopwatch storage stormy stove strategies strategy strength
    strengths stress stressed stressful stretch stretching stroller structure structured struggle
    struggled struggling student students studied studying subject subjects subscriber subscribers
    subscription subscriptions subway success successful summarize summarized summarizing summary
    summer sunny supplement supplements supplier suppliers support supported supportive suppose
    supposed supposing surgery surprise surprised sustain sustainability sustainable sustained
    sustaining symptom symptoms sync synced syncing table tactic tactics takeout talent talents
    target targets task tasks tax taxes taxi teacher teachers teamwork tech technology teen
    teenager teens temperature tenant tense tension termination test tested testing tests texting
    texts thankful thankfulness therapist therapy thermostat thunder ticketing tidied tidy tidying
    tile tiles timeline timelines timer tire tired tires toddler todo toothbrush toothpaste topic
    topics towel towels track tracked tracker tracking traffic trained training transform
    transformed transforming trash traveled traveling treatment trips trust trusted trusting tutor
    tutoring unbalanced uncomfortable understanding university update updated updates updating
    upgrade upgraded upgrading upholstery urgent utilities vacation vacations vaccination vaccine
    vacuum value values vegetables vendor vendors verified verify verifying version versions vet
    veterinarian vision vitamin vitamins voicemail volume voluntary volunteer volunteered
    volunteering wage wages walked walking wallpaper warehouse warranty weakness weaknesses webinar
    website websites weekend weekends weekly wellbeing wellness whisk whisked whisking whiteboard
    wifi windy wine winter worker workout workouts workplace workshop workshops worried worry
    worrying writes writing written wrote xray yearly yoga
    """.split()
)


WORD_BANDS = (BAND_COMMON, BAND_FREQUENT, BAND_ORDINARY)


def band_of(word: str) -> int:
    """Return 0, 1 or 2 for the band containing `word` (0 = most common),
    or -1 when the word is in no band (treated as rare).
    """
    if not word:
        return -1
    cleaned = word.strip().lower()
    if not cleaned:
        return -1
    for index, band in enumerate(WORD_BANDS):
        if cleaned in band:
            return index
    return -1
