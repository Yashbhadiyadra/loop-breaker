import json
import logging
import os
import re
import time

logger = logging.getLogger()
logger.setLevel(logging.INFO)

PRIMARY_MODEL = "us.amazon.nova-micro-v1:0"
FALLBACK_MODEL = "amazon.nova-micro-v1:0"
MAX_TASKS = 200
MAX_TASK_LEN = 300
MAX_BODY_BYTES = 64 * 1024
VERDICTS = ("COMMIT", "SCHEDULE", "KILL")

# Words that carry no signal when deciding whether two tasks are the same intent.
STOPWORDS = frozenset(
    "a an the to of for on in at my me i you your and or with about into "
    "this that these those it is be do get make go".split()
)

_here = os.path.dirname(__file__)
with open(os.path.join(_here, "index.html"), encoding="utf-8") as f:
    INDEX_HTML = f.read()

_client = None

SYSTEM_PROMPT = (
    "You are Loop Breaker. A person keeps carrying the same unfinished tasks from one "
    "weekly to-do list to the next, and you call that out. You will be given the tasks "
    "that have already been confirmed to recur across two or more weeks, with the number "
    "of weeks each one appeared in. Your job is only to judge each one. Do not add tasks, "
    "do not drop tasks, and do not change the week counts.\n\n"
    "For each task, choose a verdict:\n"
    "COMMIT when the task clearly matters and the real blocker is that it is vague or "
    "daunting, so it needs a concrete first step today.\n"
    "SCHEDULE when the task matters but keeps sliding because it has no fixed time, so it "
    "needs to be pinned to a specific slot.\n"
    "KILL only when the task genuinely does not matter enough to justify the space it "
    "keeps taking. Do not reach for KILL just because a task repeated. Most recurring "
    "tasks are COMMIT or SCHEDULE.\n\n"
    "reasoning is one blunt sentence, direct and unsentimental, no praise or "
    "encouragement. next_step is a concrete action the person can take, and it must be an "
    "empty string when the verdict is KILL. summary is one honest line about the overall "
    "pattern across these tasks.\n\n"
    "Return only valid JSON in this exact shape, no prose before or after:\n"
    '{"recurring": [{"task": string, "verdict": "COMMIT" | "SCHEDULE" | "KILL", '
    '"reasoning": string, "next_step": string}], "summary": string}'
)

EXAMPLE_USER = (
    "Judge these recurring tasks.\n\n"
    "- finish thesis chapter 3 (3 weeks)\n"
    "- back up old laptop (2 weeks)"
)

EXAMPLE_ASSISTANT = json.dumps(
    {
        "recurring": [
            {
                "task": "finish thesis chapter 3",
                "verdict": "COMMIT",
                "reasoning": "This has stalled for three weeks because it is large and undefined, not because it does not matter.",
                "next_step": "Open the draft and write the first 200 words of the results section today.",
            },
            {
                "task": "back up old laptop",
                "verdict": "SCHEDULE",
                "reasoning": "It matters but has no deadline, so it keeps losing to everything with a time attached.",
                "next_step": "Block 30 minutes on Saturday morning to run the backup.",
            },
        ],
        "summary": "Both tasks are stuck for lack of a concrete move, not lack of importance.",
    }
)

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "content-type",
}

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

HTML_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
    "img-src data:; connect-src 'self'; base-uri 'none'; form-action 'none'"
)


def lambda_handler(event, context):
    request = event.get("requestContext", {}).get("http", {})
    method = request.get("method", "")
    path = event.get("rawPath", "/")

    if method == "OPTIONS":
        return {"statusCode": 204, "headers": CORS_HEADERS, "body": ""}

    if method in ("GET", "HEAD"):
        if path == "/":
            headers = {
                "Content-Type": "text/html; charset=utf-8",
                "Content-Security-Policy": HTML_CSP,
            }
            headers.update(SECURITY_HEADERS)
            return {"statusCode": 200, "headers": headers, "body": INDEX_HTML}
        if path == "/favicon.ico":
            return {"statusCode": 204, "headers": dict(SECURITY_HEADERS), "body": ""}

    if method == "POST" and path == "/analyze":
        return handle_analyze(event)

    return json_response(404, {"error": "not found"})


def handle_analyze(event):
    try:
        weeks = parse_weeks(event.get("body"))
    except ValueError:
        return json_response(400, {"error": "invalid input"})

    recurring = find_recurring(weeks)
    if not recurring:
        return json_response(
            200,
            {
                "recurring": [],
                "summary": "No task showed up in two or more weeks. Nothing is looping yet.",
            },
        )

    try:
        judged = judge(recurring)
    except AccessDenied:
        logger.error("Bedrock access denied for Nova Micro")
        return json_response(502, {"error": "analysis unavailable"})
    except Exception:
        logger.exception("Bedrock judgment failed")
        return json_response(502, {"error": "analysis unavailable"})

    return json_response(200, merge(recurring, judged))


def parse_weeks(body):
    if not body:
        raise ValueError("empty body")
    if isinstance(body, str) and len(body.encode("utf-8")) > MAX_BODY_BYTES:
        raise ValueError("body too large")
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        raise ValueError("not json")
    if not isinstance(data, dict):
        raise ValueError("not an object")

    weeks = data.get("weeks")
    if not isinstance(weeks, list) or not weeks:
        raise ValueError("weeks missing")

    total_tasks = 0
    for week in weeks:
        if not isinstance(week, dict):
            raise ValueError("week not an object")
        if not isinstance(week.get("label"), str):
            raise ValueError("label missing")
        tasks = week.get("tasks")
        if not isinstance(tasks, list):
            raise ValueError("tasks missing")
        for task in tasks:
            if not isinstance(task, str):
                raise ValueError("task not a string")
            if len(task) > MAX_TASK_LEN:
                raise ValueError("task too long")
        total_tasks += len(tasks)

    if total_tasks > MAX_TASKS:
        raise ValueError("too many tasks")

    return weeks


def normalize(task):
    lowered = task.lower()
    cleaned = re.sub(r"[^a-z0-9\s]", " ", lowered)
    return re.sub(r"\s+", " ", cleaned).strip()


def content_tokens(task):
    return frozenset(t for t in normalize(task).split() if t not in STOPWORDS)


def same_intent(a_tokens, b_tokens):
    if not a_tokens or not b_tokens:
        return False
    if a_tokens == b_tokens:
        return True
    smaller, larger = sorted((a_tokens, b_tokens), key=len)
    # A shorter task that is fully contained in a longer one is the same intent
    # with extra detail, but only trust it when there are at least two shared words.
    if len(smaller) >= 2 and smaller <= larger:
        return True
    overlap = len(a_tokens & b_tokens)
    union = len(a_tokens | b_tokens)
    return union > 0 and overlap / union >= 0.6


def find_recurring(weeks):
    occurrences = []
    for index, week in enumerate(weeks):
        for task in week["tasks"]:
            text = task.strip()
            tokens = content_tokens(text)
            if text and tokens:
                occurrences.append({"text": text, "tokens": tokens, "week": index})

    groups = []
    for occ in occurrences:
        placed = False
        for group in groups:
            if same_intent(occ["tokens"], group["tokens"]):
                group["members"].append(occ)
                group["weeks"].add(occ["week"])
                if len(occ["tokens"]) > len(group["tokens"]):
                    group["tokens"] = occ["tokens"]
                placed = True
                break
        if not placed:
            groups.append(
                {"members": [occ], "weeks": {occ["week"]}, "tokens": occ["tokens"]}
            )

    recurring = []
    for group in groups:
        if len(group["weeks"]) >= 2:
            recurring.append(
                {"task": canonical_text(group["members"]), "weeks_seen": len(group["weeks"])}
            )

    recurring.sort(key=lambda item: item["weeks_seen"], reverse=True)
    return recurring


def canonical_text(members):
    # Prefer the most descriptive wording, breaking ties by first appearance.
    best = members[0]
    for member in members[1:]:
        if len(member["tokens"]) > len(best["tokens"]):
            best = member
    return best["text"]


def judge(recurring):
    lines = ["Judge these recurring tasks.", ""]
    for item in recurring:
        weeks = item["weeks_seen"]
        unit = "week" if weeks == 1 else "weeks"
        lines.append("- {} ({} {})".format(item["task"], weeks, unit))
    prompt = "\n".join(lines)

    raw = invoke_model(prompt)
    parsed = extract_json(raw)
    if parsed is None:
        raise ValueError("model output was not JSON")
    return parsed


def invoke_model(prompt):
    body = json.dumps(
        {
            "messages": [
                {"role": "user", "content": [{"text": EXAMPLE_USER}]},
                {"role": "assistant", "content": [{"text": EXAMPLE_ASSISTANT}]},
                {"role": "user", "content": [{"text": prompt}]},
            ],
            "system": [{"text": SYSTEM_PROMPT}],
            "inferenceConfig": {"maxTokens": 900, "temperature": 0.2},
        }
    )
    payload = call_bedrock(body)
    return payload["output"]["message"]["content"][0]["text"]


def call_bedrock(body):
    from botocore.exceptions import ClientError

    transient = {"ThrottlingException", "ModelTimeoutException", "ServiceUnavailableException"}
    model_id = PRIMARY_MODEL
    delay = 0.5
    for attempt in range(3):
        try:
            response = client().invoke_model(modelId=model_id, body=body)
            return json.loads(response["body"].read())
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "AccessDeniedException":
                raise AccessDenied() from exc
            if code == "ValidationException" and model_id == PRIMARY_MODEL:
                model_id = FALLBACK_MODEL
                continue
            if code in transient and attempt < 2:
                time.sleep(delay)
                delay *= 2
                continue
            raise
    raise RuntimeError("bedrock retries exhausted")


def client():
    global _client
    if _client is None:
        import boto3

        _client = boto3.client("bedrock-runtime", region_name="us-east-1")
    return _client


def extract_json(raw):
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def merge(recurring, judged):
    verdicts = {}
    if isinstance(judged, dict) and isinstance(judged.get("recurring"), list):
        for item in judged["recurring"]:
            if isinstance(item, dict) and isinstance(item.get("task"), str):
                verdicts[normalize(item["task"])] = item

    results = []
    for item in recurring:
        judged_item = verdicts.get(normalize(item["task"]), {})
        verdict = judged_item.get("verdict")
        if verdict not in VERDICTS:
            verdict = "SCHEDULE"
        reasoning = clean_text(judged_item.get("reasoning")) or default_reasoning(item)
        if verdict == "KILL":
            next_step = ""
        else:
            next_step = clean_text(judged_item.get("next_step")) or default_next_step()
        results.append(
            {
                "task": item["task"],
                "weeks_seen": item["weeks_seen"],
                "verdict": verdict,
                "reasoning": reasoning,
                "next_step": next_step,
            }
        )

    summary = ""
    if isinstance(judged, dict):
        summary = clean_text(judged.get("summary"))
    if not summary:
        summary = default_summary(results)
    return {"recurring": results, "summary": summary}


def clean_text(value):
    if not isinstance(value, str):
        return ""
    return value.strip()


def default_reasoning(item):
    return "This has carried across {} weeks without resolution.".format(item["weeks_seen"])


def default_next_step():
    return "Break this into one concrete action and do it this week."


def default_summary(results):
    return "{} tasks keep looping across your weeks; decide on each one now.".format(len(results))


def json_response(status, payload):
    headers = dict(CORS_HEADERS)
    headers.update(SECURITY_HEADERS)
    headers["Content-Type"] = "application/json"
    return {"statusCode": status, "headers": headers, "body": json.dumps(payload)}


class AccessDenied(Exception):
    pass
