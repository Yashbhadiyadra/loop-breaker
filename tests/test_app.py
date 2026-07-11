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
