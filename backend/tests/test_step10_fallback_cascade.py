"""Phase 4 Step 10: fallback cascade + circuit breaker.

Providers are fake generators and the breaker runs on a fake clock, so
these need no API key and no sleeping. The standalone module run
(python -m app.fallback_cascade) repeats the core checks and adds a live
cut of a real Anthropic stream — README Step 10 section.
"""

import pytest

from app import fallback_cascade as fc


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def fake(chunks, fail_at=None, log=None):
    def stream(messages):
        if log is not None:
            log.append(messages)
        for i, chunk in enumerate(chunks):
            if fail_at is not None and i == fail_at:
                raise fc.ProviderFailure(f"provider error at chunk {i}")
            yield chunk

    return stream


def cascade(primary, secondary, tertiary=None, models=None, clock=None):
    breakers = {p: fc.CircuitBreaker(p, failure_threshold=3, recovery_seconds=30, clock=clock or FakeClock()) for p in fc.TIER_ORDER}
    return fc.FallbackCascade(
        providers={"primary": primary, "secondary": secondary, "tertiary": tertiary or fake(["kb answer"])},
        models=models or {"primary": "model-a", "secondary": "model-b", "tertiary": "local"},
        breakers=breakers,
    )


# ---- circuit breaker --------------------------------------------------------


def test_breaker_opens_after_threshold_and_blocks_requests():
    breaker = fc.CircuitBreaker("primary", failure_threshold=3, recovery_seconds=30, clock=FakeClock())
    for _ in range(2):
        breaker.record_failure()
    assert breaker.state == "closed" and breaker.allow_request()

    breaker.record_failure()

    assert breaker.state == "open" and not breaker.allow_request()
    assert breaker.times_opened == 1


def test_a_success_resets_the_consecutive_failure_count():
    breaker = fc.CircuitBreaker("primary", failure_threshold=3, recovery_seconds=30, clock=FakeClock())
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()

    assert breaker.state == "closed"


def test_half_open_trial_failure_reopens_and_restarts_the_cool_down():
    clock = FakeClock()
    breaker = fc.CircuitBreaker("primary", failure_threshold=3, recovery_seconds=30, clock=clock)
    for _ in range(3):
        breaker.record_failure()
    clock.now = 30
    assert breaker.state == "half_open" and breaker.allow_request()

    breaker.record_failure()  # the single trial fails

    assert breaker.state == "open"
    clock.now = 59
    assert breaker.state == "open"  # cool-down restarted at t=30
    clock.now = 60
    assert breaker.state == "half_open"
    breaker.record_success()
    assert breaker.state == "closed"
    assert breaker.times_opened == 2


# ---- cascade ----------------------------------------------------------------


def test_mid_stream_failure_hot_swaps_and_attributes_every_chunk():
    c = cascade(fake(["Forget ", "and ", "rejoin ", "then "], fail_at=3), fake(["disable ", "power-saving."]))

    events = list(c.stream("wifi drops"))
    chunks = [e for e in events if isinstance(e, fc.Chunk)]
    [switch] = [e for e in events if isinstance(e, fc.TierSwitch)]

    assert [(ch.tier, ch.text) for ch in chunks] == [
        ("primary", "Forget "),
        ("primary", "and "),
        ("primary", "rejoin "),
        ("secondary", "disable "),
        ("secondary", "power-saving."),
    ]
    assert (switch.from_tier, switch.to_tier, switch.chars_already_sent) == ("primary", "secondary", len("Forget and rejoin "))
    assert events.index(switch) == 3  # the switch sits exactly between the two tiers' chunks


def test_the_next_tier_is_told_what_was_already_sent():
    seen = []
    c = cascade(fake(["Step 1: forget the network. ", "Step 2"], fail_at=1), fake(["…continued"], log=seen))

    list(c.stream("wifi drops"))

    prompt = seen[0][-1].content
    assert "PARTIAL ANSWER ALREADY SENT:\nStep 1: forget the network. " in prompt
    assert "Do not repeat" in prompt and "STUDENT QUESTION:\nwifi drops" in prompt
    assert "Begin directly with the next word" in prompt  # partial ended on whitespace: no anchor to echo


def test_a_mid_word_cut_never_reaches_the_student():
    # Demo-page finding: a cut after "select C" made the next model guess the
    # word from one letter ("C" + "ompusSecure", "CAMPUSNET"). The partial
    # word is now held back until whitespace, so it's dropped, never sent.
    seen = []
    c = cascade(fake(['Go to Wi-Fi settings, select "Camp', 'usSecure" now'], fail_at=1), fake(["CampusSecure"], log=seen))

    transcript = fc.collect(c.stream("wifi drops"))
    [switch] = [e for e in transcript.events if isinstance(e, fc.TierSwitch)]

    assert transcript.segments[0][2] == "Go to Wi-Fi settings, select "  # '"Camp' never sent
    assert transcript.text == "Go to Wi-Fi settings, select CampusSecure"
    assert "unsent partial word '\"Camp' dropped" in switch.reason
    assert c.health_report()["primary"]["chars_withheld"] == len('"Camp')
    prompt = seen[0][-1].content
    assert prompt.rstrip().endswith("STUDENT QUESTION:\nwifi drops")
    assert "Begin directly with the next word" in prompt  # clean boundary: nothing to echo


def test_text_is_released_word_by_word_and_flushed_when_the_tier_finishes():
    c = cascade(fake(["Forget an", "d rejoin Campus", "Secure"]), fake(["unused"]))

    chunks = [e.text for e in c.stream("q") if isinstance(e, fc.Chunk)]

    assert chunks == ["Forget ", "and rejoin ", "CampusSecure"]


# ---- seam trimming (Step 11 drill finding) ----------------------------------


@pytest.mark.parametrize(
    "sent, echoed_chunks, expected_seam",
    [
        # word boundary: the model echoes "and", trimming restores the space
        ("2. **Forget and", ["and", " rejoin the network**"], "2. **Forget and rejoin the network**"),
        ("– Go", ["Go to ", "Settings"], "– Go to Settings"),
        # mid-word cut: the model completes the word, trimming keeps only the rest
        ('select "Campus', ['"CampusSecure"', " from the list"], 'select "CampusSecure" from the list'),
        ("select C", ["CampusSecure, then"], "select CampusSecure, then"),
        # punctuation-ended partial: echo "enabled." then the new paragraph survives
        ("Wi-Fi is enabled.", ["enabled.", "\n\n2. Restart"], "Wi-Fi is enabled.\n\n2. Restart"),
        # the echo arrives split across chunks and with leading whitespace
        ("Forget and", [" an", "d rejoin"], "Forget and rejoin"),
    ],
)
def test_trim_seam_restores_exact_spacing(sent, echoed_chunks, expected_seam):
    unverified = []

    out = "".join(fc._trim_seam(iter(echoed_chunks), fc.seam_anchor(sent), lambda: unverified.append(1)))

    assert sent + out == expected_seam
    assert unverified == []


def test_trim_seam_passes_through_and_counts_when_the_model_did_not_echo():
    unverified = []

    out = "".join(fc._trim_seam(iter(["rejoin the network"]), "and", lambda: unverified.append(1)))

    assert out == "rejoin the network"
    assert unverified == [1]


def test_echoing_model_tier_gets_its_seam_trimmed_end_to_end():
    secondary = fake(["and", " rejoin the network."])
    secondary.echoes_seam = True  # what real model tiers set
    c = cascade(fake(["Forget ", "and", " more"], fail_at=2), secondary)

    transcript = fc.collect(c.stream("wifi drops"))

    assert transcript.text == "Forget and rejoin the network."
    assert c.health_report()["secondary"]["seams_unverified"] == 0


def test_local_kb_tier_continuing_an_answer_starts_a_new_paragraph():
    c = cascade(fake(["Forget ", "and", " more"], fail_at=2), fake(["y"], fail_at=0), tertiary=fc._local_kb_stream)

    transcript = fc.collect(c.stream("My Wi-Fi keeps dropping in the library"))

    # "and" was mid-flight (no trailing space yet) when the primary failed, so
    # it was never sent; the local tier starts a new paragraph after "Forget ".
    assert transcript.text.startswith("Forget \n\nOur assistant is temporarily unavailable")


def test_first_tier_gets_the_plain_question():
    seen = []
    c = cascade(fake(["ok"], log=seen), fake(["unused"]))

    list(c.stream("wifi drops"))

    assert seen[0][-1].content == "wifi drops"


def test_open_circuit_tier_is_skipped_without_a_request():
    calls = []
    c = cascade(fake(["never"], log=calls), fake(["from secondary"]))
    for _ in range(3):
        c.breakers["primary"].record_failure()

    transcript = fc.collect(c.stream("q"))

    assert calls == []
    assert transcript.served_by == ["secondary"]
    assert isinstance(transcript.events[0], fc.TierSkipped)
    assert c.health_report()["primary"]["skipped_open"] == 1


def test_repeated_failures_trip_the_breaker_so_later_requests_skip_primary():
    calls = []
    c = cascade(fake(["x"], fail_at=0, log=calls), fake(["ok"]))

    for _ in range(5):
        fc.collect(c.stream("q"))

    assert len(calls) == 3  # attempts 4 and 5 never reached the failing provider
    assert c.health_report()["primary"]["circuit"] == "open"
    assert c.health_report()["primary"]["skipped_open"] == 2


def test_tiers_sharing_a_model_keep_separate_health_and_breakers():
    c = cascade(fake(["a ", "b "], fail_at=1), fake(["c"]), models={"primary": "same", "secondary": "same", "tertiary": "local"})

    transcript = fc.collect(c.stream("q"))
    health = c.health_report()

    assert transcript.served_by == ["primary", "secondary"]
    assert (health["primary"]["failures"], health["secondary"]["failures"]) == (1, 0)
    assert (health["primary"]["chars_served"], health["secondary"]["chars_served"]) == (2, 1)
    assert c.breakers["primary"] is not c.breakers["secondary"]


def test_both_models_down_falls_through_to_the_local_kb_responder():
    c = cascade(fake(["x"], fail_at=0), fake(["y"], fail_at=0), tertiary=fc._local_kb_stream)

    transcript = fc.collect(c.stream("My Wi-Fi keeps dropping in the library"))

    assert transcript.served_by == ["tertiary"]
    assert "KB-102" in transcript.text


def test_every_tier_down_ends_the_stream_with_a_terminal_switch():
    c = cascade(fake(["x"], fail_at=0), fake(["y"], fail_at=0), tertiary=fake(["z"], fail_at=0))

    transcript = fc.collect(c.stream("q"))

    assert transcript.text == ""
    assert [(e.from_tier, e.to_tier) for e in transcript.events] == [
        ("primary", "secondary"),
        ("secondary", "tertiary"),
        ("tertiary", None),
    ]


@pytest.mark.parametrize("after, primary_chars", [(0, 0), (2, 4)])
def test_fault_plan_cuts_a_tier_and_closes_its_stream(after, primary_chars):
    closed = []

    def closable(messages):
        try:
            yield from ["a ", "b ", "c ", "d "]
        finally:
            closed.append(True)

    c = cascade(closable, fake(["rest"]))

    transcript = fc.collect(c.stream("q", fc.FaultPlan(after_chunks={"primary": after})))

    assert c.health_report()["primary"]["chars_served"] == primary_chars
    assert transcript.text.endswith("rest")
    assert closed == ([] if after == 0 else [True])  # after=0 fails before the provider is even called
