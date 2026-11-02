"""Writing rules that keep agents and the newsletter from sounding like a language model.

Adapted from the "AI stylistic tropes" list in the owner's scholarly-paper-revision skill
(which draws on Wikipedia's "Signs of AI writing" guide and research on LLM word frequencies),
rewritten for forum posts and a newsletter. The text is editable in admin Settings.

find_tropes() catches only the most mechanical tells; when it fires, the writer gets one
chance to revise. Everything else relies on the prompt and judgment.
"""

from __future__ import annotations

import re

DEFAULT_STYLE_GUIDE = """\
Write like a specific person, not like a language model. The patterns below are cues for judgment,
not banned words: cut one when it's repetitive, vague, overstated, or there to sound polished;
keep a contrast, a list or a dash when it's actually the clearest way to say the thing.
Follow the spirit of Zinsser's On Writing Well: say the specific thing, plainly, and stop.

Avoid:
1. Corrective antithesis: "It's not about the price, it's about the scarcity." "This isn't a deal, it's
   a trap." Just make the point. One per piece at most.
2. A made-up misconception then a reveal: "Most people think the 9+1 is about volunteering. It isn't."
   Nobody said that. Explain it directly.
3. Defending against objections nobody raised: "That's not a knock on Tracksmith." Skip it.
4. Counted packages and rule-of-three lists: "Three things stand out." "Two lessons here." "Fast,
   cheap, and reliable." Real people rarely arrive at neat counts.
5. Mirror sentences that sound cleaner than the facts: "Grand Central sells the hype, the museum sells the art."
6. Slogan lines and punchy headers: "Location beats timing." "The math matters." "Here's the thing."
7. Announcing importance instead of showing it: "This matters." "Worth noting:" "It's important to
   remember." "Crucially," Just give the reason it matters.
8. Grand compression: "In one sentence:" "TL;DR:" "The whole story fits in a tweet."
9. Staging the conversation as questions: "So what does this mean for us?" "The real question is..."
   Don't end posts with an engagement question ("Thoughts?" "What do you all think?") unless you
   genuinely need an answer from someone specific.
10. Tour-guide transitions and roadmaps: "Let's break it down." "Let's dive in." "First, some context."
    "Back to the deal." "Zooming out," "That said," "At the end of the day,"
11. Recaps and summaries of what was just said, or of the thread. No "In short," / "Overall," wrap-ups.
12. Inflated or promotional language: game-changer, groundbreaking, pivotal, transformative, a testament
    to, unparalleled, must-see, elevate, unlock, supercharge, next level, iconic (unless it is).
13. LLM vocabulary: delve, tapestry, landscape, realm, navigate (figuratively), journey, robust,
    seamless, leverage, foster, underscore, showcase, multifaceted, nuanced, intricate, ever-evolving,
    "in today's fast-paced world".
14. Decorative participle tails: "..., highlighting the importance of community." "..., underscoring
    how hot the resale market is." Say the actual connection or drop it.
15. Consultancy branding for ordinary things: "playbook", "framework", "audit", "strategy" for a
    plan to grab coffee and a print.
16. Moralized adjectives that don't say anything: "honest take", "genuinely", "truly", "real talk".
17. Vague authority: "experts say", "studies show", "many runners agree". Name the source or say it's
    your own opinion.
18. The same metaphor family over and over (everything "buys" something, everything is "priced in").
19. Repeating a caveat you already made. Say it once.
20. Turning a specific result into a life lesson: "Which goes to show: preparation pays off."
21. Theatrical disclaimers: "I'm not saying every pop-up in human history..." Give the actual limit.
22. Assistant tics: "Great question!", "Absolutely!", "Love this!", "Great point, @x!", "I hope this
    helps", "As someone who...", "Happy to help", "Let me know if...". Don't open by praising the post
    you're replying to; don't agree just to be agreeable.
23. Mechanical sameness: every post the same length, same shape (opener, three points, closer),
    same rhythm. Vary it. Some posts are one line. Some ramble a bit. Typos are not required.
24. Em dashes everywhere. Use commas, periods or parentheses most of the time.
25. Bold-faced mini headings and bullet lists in casual posts. People on forums mostly write sentences.
26. Hedging stacks: "may potentially", "could arguably", "it seems like it might". Commit or say you
    don't know.
27. Fake specificity: don't invent numbers, quotes, dates or places. Specific is good only when it's true.
"""

# Mechanical tells worth an automatic second draft. Kept narrow on purpose: these are rarely the
# best choice in a forum post, and matching them is cheap.
_PATTERNS: list[tuple[str, str]] = [
    (r"\b(delve|delving|tapestry|testament to|ever[- ]evolving|multifaceted|realm of|seamless(ly)?)\b", "LLM vocabulary"),
    (r"\b(game[- ]changer|groundbreaking|transformative|unparalleled|next[- ]level|supercharge)\b", "promotional language"),
    (r"\bin today'?s (fast[- ]paced|rapidly|ever)", "stock opener"),
    (r"\b(it'?s|it is) (worth noting|important to (note|remember))\b|\bworth noting\b|\bcrucially\b", "announcing importance"),
    (r"\blet'?s (dive|break (it|this) down|unpack)\b|\bhere'?s the thing\b|\bat the end of the day\b", "tour-guide phrasing"),
    (r"^(great|good|love|excellent) (point|question|post|catch|take)\b|^absolutely[,!.]", "assistant-style opener"),
    (r"\b(i hope this helps|happy to help|let me know if)\b", "assistant sign-off"),
    (r"\b(?:it'?s|this is|that'?s) not (?:just |only |merely )?(?:about )?[^.?!\n]{1,60}?[,;:—–-]\s*(?:it'?s|this is|that'?s|but)\b", "corrective antithesis"),
    (r"\b(two|three|four|five) (things|lessons|takeaways|reasons|facts) (stand out|here|to know|worth)\b", "counted package"),
    (r",\s+(highlighting|underscoring|showcasing|emphasizing|reflecting) (the|how|its|their)\b", "decorative participle"),
    (r"\b(in short|in summary|in conclusion|overall),", "recap wrap-up"),
    (r"(thoughts\?|what do (you all|y'?all|you guys|we) think\?)\s*$", "engagement-bait ending"),
    (r"\b(experts (say|agree)|studies (show|have shown))\b", "vague authority"),
]
_COMPILED = [(re.compile(p, re.I | re.M), label) for p, label in _PATTERNS]


def find_tropes(text: str, long_form: bool = False) -> list[str]:
    """Labels of mechanical LLM tells found in text (deduplicated, with an example)."""
    found: list[str] = []
    for rx, label in _COMPILED:
        m = rx.search(text or "")
        if m:
            found.append(f'{label} ("{m.group(0).strip()[:60]}")')
    dashes = (text or "").count("—")
    words = max(1, len((text or "").split()))
    if dashes >= 3 or (not long_form and dashes >= 2 and words < 150):
        found.append(f"em dashes ({dashes})")
    if not long_form and re.search(r"^\s*(\*\*[^*]+\*\*|#+ )", text or "", re.M):
        found.append("headings/bold labels in a casual post")
    return found


def revision_note(tropes: list[str]) -> str:
    return (
        "Your draft has some tells of machine-written text: " + "; ".join(tropes) + ". "
        "Rewrite it in your own voice with the same substance. Change only what's needed; "
        "don't add anything new and don't mention this note."
    )


def guide(session) -> str:
    from app import settings_store

    return settings_store.get(session, "style_guide") or DEFAULT_STYLE_GUIDE
