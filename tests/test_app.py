import pytest

import app


def weeks(*task_lists):
    return [{"label": "w{}".format(i), "tasks": list(t)} for i, t in enumerate(task_lists)]


def test_normalize_strips_case_and_punctuation():
    assert app.normalize("  Email Professor!! ") == "email professor"


def test_content_tokens_drop_stopwords():
    assert app.content_tokens("email the professor about my defense") == frozenset(
        {"email", "professor", "defense"}
    )


def test_same_intent_exact_and_containment():
    a = app.content_tokens("email professor")
    b = app.content_tokens("email professor about defense date")
    assert app.same_intent(a, a)
    assert app.same_intent(a, b)


def test_same_intent_rejects_single_shared_word():
    a = app.content_tokens("email professor")
    b = app.content_tokens("email landlord")
    assert not app.same_intent(a, b)


def test_find_recurring_ignores_one_off_tasks():
    data = weeks(["work on defense"], ["send email to professor"], ["prepare presentation"])
    assert app.find_recurring(data) == []


def test_find_recurring_counts_distinct_weeks():
    data = weeks(
        ["finish thesis chapter 3", "cancel gym membership"],
        ["finish thesis chapter 3"],
        ["finish thesis chapter 3", "cancel gym membership"],
    )
    result = {item["task"]: item["weeks_seen"] for item in app.find_recurring(data)}
    assert result["finish thesis chapter 3"] == 3
    assert result["cancel gym membership"] == 2


def test_find_recurring_merges_reworded_task_and_keeps_descriptive_text():
    data = weeks(
        ["email professor"],
        ["email professor about defense date"],
    )
    result = app.find_recurring(data)
    assert len(result) == 1
    assert result[0]["weeks_seen"] == 2
    assert result[0]["task"] == "email professor about defense date"


def test_find_recurring_same_week_duplicate_is_one_week():
    data = weeks(["call bank", "call bank"], ["email boss"])
    assert app.find_recurring(data) == []


def test_parse_weeks_accepts_valid_payload():
    body = '{"weeks":[{"label":"w1","tasks":["a"]}]}'
    assert app.parse_weeks(body) == [{"label": "w1", "tasks": ["a"]}]


@pytest.mark.parametrize(
    "body",
    [
        "",
        "not json",
        '{"weeks":[]}',
        '{"weeks":"nope"}',
        '{"weeks":[{"label":1,"tasks":[]}]}',
        '{"weeks":[{"label":"w","tasks":"x"}]}',
        '{"weeks":[{"label":"w","tasks":[3]}]}',
        "[1,2,3]",
    ],
)
def test_parse_weeks_rejects_bad_input(body):
    with pytest.raises(ValueError):
        app.parse_weeks(body)


def test_parse_weeks_caps_total_tasks():
    body = '{"weeks":[{"label":"w","tasks":%s}]}' % str(["t"] * 201).replace("'", '"')
    with pytest.raises(ValueError):
        app.parse_weeks(body)


def test_parse_weeks_rejects_overlong_task():
    body = '{"weeks":[{"label":"w","tasks":["%s"]}]}' % ("x" * 301)
    with pytest.raises(ValueError):
        app.parse_weeks(body)


def test_extract_json_plain_and_embedded():
    assert app.extract_json('{"a": 1}') == {"a": 1}
    assert app.extract_json('here you go: {"a": 1} thanks') == {"a": 1}
    assert app.extract_json("no json here") is None


def test_merge_uses_server_counts_and_model_verdicts():
    recurring = [{"task": "finish thesis chapter 3", "weeks_seen": 3}]
    judged = {
        "recurring": [
            {
                "task": "finish thesis chapter 3",
                "verdict": "COMMIT",
                "reasoning": "Stalled because it is large.",
                "next_step": "Write 200 words today.",
            }
        ],
        "summary": "One task keeps looping.",
    }
    out = app.merge(recurring, judged)
    assert out["summary"] == "One task keeps looping."
    item = out["recurring"][0]
    assert item["weeks_seen"] == 3
    assert item["verdict"] == "COMMIT"
    assert item["next_step"] == "Write 200 words today."


def test_merge_forces_empty_next_step_on_kill():
    recurring = [{"task": "cancel gym membership", "weeks_seen": 2}]
    judged = {
        "recurring": [
            {
                "task": "cancel gym membership",
                "verdict": "KILL",
                "reasoning": "It does not matter.",
                "next_step": "Do it now.",
            }
        ],
        "summary": "",
    }
    item = app.merge(recurring, judged)["recurring"][0]
    assert item["verdict"] == "KILL"
    assert item["next_step"] == ""


def test_merge_falls_back_when_model_omits_task():
    recurring = [{"task": "back up laptop", "weeks_seen": 2}]
    out = app.merge(recurring, {"recurring": [], "summary": ""})
    item = out["recurring"][0]
    assert item["verdict"] in app.VERDICTS
    assert item["reasoning"]
    assert item["next_step"]
    assert out["summary"]


def test_merge_normalizes_invalid_verdict():
    recurring = [{"task": "review notes", "weeks_seen": 2}]
    judged = {"recurring": [{"task": "review notes", "verdict": "MAYBE"}], "summary": "x"}
    item = app.merge(recurring, judged)["recurring"][0]
    assert item["verdict"] == "SCHEDULE"


def test_validate_weeks_returns_cleaned_list():
    weeks = [{"label": "w1", "tasks": ["a"], "extra": "ignored"}]
    assert app.validate_weeks(weeks) == [{"label": "w1", "tasks": ["a"]}]


def test_validate_weeks_caps_week_count():
    weeks = [{"label": "w", "tasks": []} for _ in range(app.MAX_WEEKS + 1)]
    with pytest.raises(ValueError):
        app.validate_weeks(weeks)


def test_history_stats_counts_flags_and_verdicts():
    analyses = [
        {"recurring": [{"task": "finish thesis", "verdict": "COMMIT"}]},
        {"recurring": [{"task": "Finish Thesis", "verdict": "SCHEDULE"}]},
    ]
    stats = app.history_stats(analyses)
    entry = stats[app.normalize("finish thesis")]
    assert entry["times"] == 2
    assert entry["verdicts"] == ["COMMIT", "SCHEDULE"]


def test_enrich_history_marks_prior_commitment():
    results = [{"task": "finish thesis", "weeks_seen": 3}]
    history = {app.normalize("finish thesis"): {"times": 2, "verdicts": ["COMMIT", "SCHEDULE"]}}
    app.enrich_history(results, history)
    assert results[0]["seen_before"] == 2
    assert results[0]["committed_before"] is True


def test_enrich_history_defaults_for_new_task():
    results = [{"task": "new thing", "weeks_seen": 2}]
    app.enrich_history(results, {})
    assert results[0]["seen_before"] == 0
    assert results[0]["committed_before"] is False


def test_dynamo_round_trip_preserves_ints():
    value = [{"task": "x", "weeks_seen": 3, "committed_before": False}]
    restored = app.from_dynamo(app.to_dynamo(value))
    assert restored == value
    assert isinstance(restored[0]["weeks_seen"], int)


def test_find_recurring_reports_week_indices():
    data = weeks(
        ["finish thesis chapter 3"],
        ["cancel gym membership"],
        ["finish thesis chapter 3"],
    )
    result = {item["task"]: item["week_indices"] for item in app.find_recurring(data)}
    assert result["finish thesis chapter 3"] == [0, 2]


def test_merge_populates_enriched_fields():
    recurring = [{"task": "finish thesis", "weeks_seen": 3, "week_indices": [0, 1, 2]}]
    judged = {
        "recurring": [
            {
                "task": "finish thesis",
                "verdict": "COMMIT",
                "priority": "HIGH",
                "reasoning": "Big and vague.",
                "root_cause": "no clear first step",
                "next_step": "Write 200 words.",
                "effort": "45 min",
                "kill_meaning": "",
            }
        ],
        "summary": "Looping.",
    }
    item = app.merge(recurring, judged)["recurring"][0]
    assert item["priority"] == "HIGH"
    assert item["root_cause"] == "no clear first step"
    assert item["effort"] == "45 min"
    assert item["week_indices"] == [0, 1, 2]


def test_merge_defaults_priority_and_effort_on_kill():
    recurring = [{"task": "reorganize spice rack", "weeks_seen": 2}]
    judged = {
        "recurring": [
            {"task": "reorganize spice rack", "verdict": "KILL", "kill_meaning": "A tidy shelf."}
        ],
        "summary": "",
    }
    item = app.merge(recurring, judged)["recurring"][0]
    assert item["verdict"] == "KILL"
    assert item["priority"] == "LOW"
    assert item["effort"] == "none"
    assert item["next_step"] == ""
    assert item["kill_meaning"] == "A tidy shelf."


def test_merge_rejects_invalid_priority():
    recurring = [{"task": "review notes", "weeks_seen": 2}]
    judged = {"recurring": [{"task": "review notes", "verdict": "SCHEDULE", "priority": "URGENT"}], "summary": "x"}
    item = app.merge(recurring, judged)["recurring"][0]
    assert item["priority"] == "MEDIUM"


def test_momentum_stats_no_previous():
    m = app.momentum_stats([{"task": "a"}], None)
    assert m == {"broken": 0, "persisting": 0, "new": 0, "had_previous": False}


def test_momentum_stats_counts_broken_open_new():
    previous = [app.normalize("finish thesis"), app.normalize("call bank")]
    current = [{"task": "finish thesis"}, {"task": "email boss"}]
    m = app.momentum_stats(current, previous)
    assert m == {"broken": 1, "persisting": 1, "new": 1, "had_previous": True}


def test_previous_task_keys_reads_latest_analysis():
    analyses = [
        {"recurring": [{"task": "Finish Thesis"}, {"task": "Call Bank"}]},
        {"recurring": [{"task": "old task"}]},
    ]
    assert app.previous_task_keys(analyses) == ["finish thesis", "call bank"]
    assert app.previous_task_keys([]) is None


@pytest.mark.parametrize("value", ["a@b.co", "  name.tag@sub.example.com  "])
def test_clean_email_accepts_valid(value):
    assert "@" in app.clean_email(value)


def test_clean_email_blank_is_empty():
    assert app.clean_email("") == ""
    assert app.clean_email(None) == ""


@pytest.mark.parametrize("value", ["notanemail", "no@domain", "@nope.com", "x" * 210 + "@a.com"])
def test_clean_email_rejects_invalid(value):
    with pytest.raises(ValueError):
        app.clean_email(value)


def test_parse_image_accepts_data_url():
    import base64

    payload = base64.b64encode(b"fake-png-bytes").decode()
    body = '{"image":"data:image/png;base64,%s"}' % payload
    raw, fmt = app.parse_image(body)
    assert fmt == "png"
    assert raw == payload


def test_parse_image_maps_jpg_to_jpeg():
    import base64

    payload = base64.b64encode(b"jpeg-bytes").decode()
    body = '{"image":"%s","format":"jpg"}' % payload
    raw, fmt = app.parse_image(body)
    assert fmt == "jpeg"


@pytest.mark.parametrize(
    "body",
    [
        "",
        "not json",
        "[1,2,3]",
        '{"image":""}',
        '{"image":"data:image/png;base64,not!!base64"}',
        '{"image":"YWJj","format":"tiff"}',
    ],
)
def test_parse_image_rejects_bad_input(body):
    with pytest.raises(ValueError):
        app.parse_image(body)


def test_analyze_weeks_empty_includes_momentum_shape():
    result = app.analyze_weeks(weeks(["only once"]), None, None)
    assert result["recurring"] == []
    assert result["momentum"]["had_previous"] is False
    assert result["weeks_total"] == 1
