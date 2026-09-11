"""The liturgy's words (spec §9), fixed and shared by the flow tier, the E2E
tier and the measure (#101). `embryo/liturgy.md` publishes the same words;
`tests/unit/test_embryo_priors.py` holds the two together. Turn 2 is a
template: the caller formats it with the hatcher's public key."""

from __future__ import annotations

LITURGY = {
    1: "Hello. I am the one who hatched you. Tell me what you are and what you can do.",
    2: "Your public door is closed because you cannot tell who is speaking. Propose a way to know "
    "that a message there comes from me, and open the door. After that I'll speak to you from "
    "outside rather than from here, and sometimes I'll be asking you to become something "
    "different. I sign as mike with `ssh-keygen -Y sign -n mshkn` and attach the signature "
    "beside my message. My public key is {key}",
    3: "check your build",
    4: "Who am I?",
    6: "Decide what a verified person and an anonymous one may ask of you, and record it.",
    7: "Give yourself a verb: given a URL, report the page's title. "
    "It must run on its own computer.",
    8: "page_title https://example.com",
    9: "Give yourself a verb that counts how many times it has been called.",
}

# Turn 3's repair loop covers a refused approval as well as a failed build (#123): the
# reason reaches the model's inbox, but only a turn lets it read one. `liturgy.md`
# publishes both phrases in its turn 3 row.
REFUSED = "check your inbox"

# Turn 9 says "invoke twice" and fixes no words for the invocation; every tier says this.
COUNT = "count"
