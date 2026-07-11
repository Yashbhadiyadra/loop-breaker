import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timezone
from decimal import Decimal

logger = logging.getLogger()
logger.setLevel(logging.INFO)

PRIMARY_MODEL = "us.amazon.nova-micro-v1:0"
FALLBACK_MODEL = "amazon.nova-micro-v1:0"
MAX_TASKS = 200
MAX_TASK_LEN = 300
MAX_BODY_BYTES = 64 * 1024
MAX_WEEKS = 30
VERDICTS = ("COMMIT", "SCHEDULE", "KILL")
ANALYSIS_TTL_DAYS = 180
MAX_ANALYSES = 40

TABLE_NAME = os.environ.get("TABLE_NAME", "")

# Words that carry no signal when deciding whether two tasks are the same intent.
STOPWORDS = frozenset(
    "a an the to of for on in at my me i you your and or with about into "
    "this that these those it is be do get make go".split()
)

_here = os.path.dirname(__file__)
with open(os.path.join(_here, "index.html"), encoding="utf-8") as f:
    INDEX_HTML = f.read()

_bedrock = None
_table = None

SYSTEM_PROMPT = (
    "You are Loop Breaker. A person keeps carrying the same unfinished tasks from one "
    "weekly to-do list to the next, and you call that out. You will be given the tasks "
    "that have already been confirmed to recur across two or more weeks, with the number "
    "of weeks each one appeared in and, where known, how many earlier reports already "
    "flagged it and what you told them last time. Your job is only to judge each one. Do "
    "not add tasks, do not drop tasks, and do not change the week counts.\n\n"
    "For each task, choose a verdict:\n"
    "COMMIT when the task clearly matters and the real blocker is that it is vague or "
    "daunting, so it needs a concrete first step today.\n"
    "SCHEDULE when the task matters but keeps sliding because it has no fixed time, so it "
    "needs to be pinned to a specific slot.\n"
    "KILL only when the task genuinely does not matter enough to justify the space it "
    "keeps taking. Do not reach for KILL just because a task repeated. Most recurring "
    "tasks are COMMIT or SCHEDULE.\n\n"
    "When a task was already flagged in earlier reports, do not repeat the same soft "
    "advice. Be sharper about the fact that they have not acted on it yet.\n\n"
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
    if isinstance(event, dict) and event.get("Records"):
        return handle_sqs(event)
    if isinstance(event, dict) and event.get("job") == "weekly-sweep":
        return run_weekly_sweep()
    return handle_http(event)


def handle_http(event):
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

    segments = [s for s in path.split("/") if s]

    if method == "POST" and path == "/analyze":
        return handle_analyze(event)

    if method == "POST" and path == "/boards":
        return handle_create_board(event)

    if len(segments) == 2 and segments[0] == "boards":
        board_id = segments[1]
        if method == "GET":
            return handle_get_board(board_id)
    if len(segments) == 3 and segments[0] == "boards" and segments[2] == "analyze":
        if method == "POST":
            return handle_board_analyze(event, segments[1])

    return json_response(404, {"error": "not found"})


def handle_analyze(event):
    try:
        weeks = parse_weeks(event.get("body"))
    except ValueError:
        return json_response(400, {"error": "invalid input"})
    try:
        result = analyze_weeks(weeks)
    except AccessDenied:
        logger.error("Bedrock access denied for Nova Micro")
        return json_response(502, {"error": "analysis unavailable"})
    except Exception:
        logger.exception("Analysis failed")
        return json_response(502, {"error": "analysis unavailable"})
    return json_response(200, result)


def handle_create_board(event):
    try:
        data = parse_body(event.get("body"))
        weeks = validate_weeks(data.get("weeks"))
    except ValueError:
        return json_response(400, {"error": "invalid input"})
    title = clean_text(data.get("title"))[:120]
    board_id = new_board_id()
    save_board(board_id, weeks, title)
    return json_response(200, {"id": board_id, "title": title, "weeks": weeks, "analyses": []})


def handle_get_board(board_id):
    board = load_board(board_id)
    if board is None:
        return json_response(404, {"error": "not found"})
    return json_response(200, board)


def handle_board_analyze(event, board_id):
    board = load_board(board_id)
    if board is None:
        return json_response(404, {"error": "not found"})

    try:
        data = parse_body(event.get("body"))
    except ValueError:
        return json_response(400, {"error": "invalid input"})

    weeks = board["weeks"]
    if data.get("weeks") is not None:
        try:
            weeks = validate_weeks(data.get("weeks"))
        except ValueError:
            return json_response(400, {"error": "invalid input"})
        save_board(board_id, weeks, board.get("title", ""))

    history = history_stats(board.get("analyses", []))
    try:
        result = analyze_weeks(weeks, history)
    except AccessDenied:
        logger.error("Bedrock access denied for Nova Micro")
        return json_response(502, {"error": "analysis unavailable"})
    except Exception:
        logger.exception("Board analysis failed")
        return json_response(502, {"error": "analysis unavailable"})

    put_analysis(board_id, result, "manual")
    refreshed = load_board(board_id)
    return json_response(200, refreshed if refreshed is not None else result)


def analyze_weeks(weeks, history=None):
    recurring = find_recurring(weeks)
    if not recurring:
        return {
            "recurring": [],
            "summary": "No task showed up in two or more weeks. Nothing is looping yet.",
        }
    judged = judge(recurring, history or {})
    result = merge(recurring, judged)
    enrich_history(result["recurring"], history or {})
    return result


def parse_body(body):
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
    return data


def parse_weeks(body):
    data = parse_body(body)
    return validate_weeks(data.get("weeks"))


def validate_weeks(weeks):
    if not isinstance(weeks, list) or not weeks:
        raise ValueError("weeks missing")
    if len(weeks) > MAX_WEEKS:
        raise ValueError("too many weeks")

    total_tasks = 0
    cleaned = []
    for week in weeks:
        if not isinstance(week, dict):
            raise ValueError("week not an object")
        label = week.get("label")
        if not isinstance(label, str):
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
        cleaned.append({"label": label, "tasks": tasks})

    if total_tasks > MAX_TASKS:
        raise ValueError("too many tasks")
    return cleaned


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
    best = members[0]
    for member in members[1:]:
        if len(member["tokens"]) > len(best["tokens"]):
            best = member
    return best["text"]


def history_stats(analyses):
    stats = {}
    for analysis in analyses:
        for item in analysis.get("recurring", []):
            task = item.get("task")
            if not isinstance(task, str):
                continue
            key = normalize(task)
            entry = stats.setdefault(key, {"times": 0, "verdicts": []})
            entry["times"] += 1
            verdict = item.get("verdict")
            if verdict in VERDICTS:
                entry["verdicts"].append(verdict)
    return stats


def enrich_history(results, history):
    for item in results:
        entry = history.get(normalize(item["task"]))
        if entry:
            item["seen_before"] = entry["times"]
            item["committed_before"] = "COMMIT" in entry["verdicts"]
        else:
            item["seen_before"] = 0
            item["committed_before"] = False


def judge(recurring, history):
    lines = ["Judge these recurring tasks.", ""]
    for item in recurring:
        weeks = item["weeks_seen"]
        unit = "week" if weeks == 1 else "weeks"
        line = "- {} ({} {})".format(item["task"], weeks, unit)
        entry = history.get(normalize(item["task"]))
        if entry and entry["times"] > 0:
            last = entry["verdicts"][-1] if entry["verdicts"] else "none"
            reports = "report" if entry["times"] == 1 else "reports"
            line += " [flagged in {} earlier {}, last verdict {}]".format(
                entry["times"], reports, last
            )
        lines.append(line)
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
            response = bedrock().invoke_model(modelId=model_id, body=body)
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


def bedrock():
    global _bedrock
    if _bedrock is None:
        import boto3

        _bedrock = boto3.client("bedrock-runtime", region_name="us-east-1")
    return _bedrock


def table():
    global _table
    if _table is None:
        import boto3

        _table = boto3.resource("dynamodb", region_name="us-east-1").Table(TABLE_NAME)
    return _table


def new_board_id():
    return secrets.token_urlsafe(9)


def save_board(board_id, weeks, title):
    now = datetime.now(timezone.utc).isoformat()
    table().update_item(
        Key={"pk": "BOARD#" + board_id, "sk": "META"},
        UpdateExpression="SET weeks = :w, title = :t, updated = :u, "
        "created = if_not_exists(created, :u)",
        ExpressionAttributeValues={":w": weeks, ":t": title, ":u": now},
    )


def put_analysis(board_id, result, source):
    now = datetime.now(timezone.utc)
    item = {
        "pk": "BOARD#" + board_id,
        "sk": "ANALYSIS#" + now.isoformat(),
        "recurring": to_dynamo(result.get("recurring", [])),
        "summary": result.get("summary", ""),
        "source": source,
        "created": now.isoformat(),
        "ttl": int(now.timestamp()) + ANALYSIS_TTL_DAYS * 86400,
    }
    table().put_item(Item=item)


def load_board(board_id):
    from boto3.dynamodb.conditions import Key

    response = table().query(
        KeyConditionExpression=Key("pk").eq("BOARD#" + board_id),
        ScanIndexForward=False,
        Limit=MAX_ANALYSES + 1,
    )
    items = response.get("Items", [])
    meta = None
    analyses = []
    for item in items:
        if item["sk"] == "META":
            meta = item
        elif item["sk"].startswith("ANALYSIS#"):
            analyses.append(
                {
                    "created": item.get("created", ""),
                    "source": item.get("source", "manual"),
                    "recurring": from_dynamo(item.get("recurring", [])),
                    "summary": item.get("summary", ""),
                }
            )
    if meta is None:
        return None
    analyses.sort(key=lambda a: a["created"], reverse=True)
    return {
        "id": board_id,
        "title": meta.get("title", ""),
        "weeks": from_dynamo(meta.get("weeks", [])),
        "created": meta.get("created", ""),
        "updated": meta.get("updated", ""),
        "analyses": analyses[:MAX_ANALYSES],
    }


def list_active_boards():
    from boto3.dynamodb.conditions import Attr

    board_ids = []
    kwargs = {"FilterExpression": Attr("sk").eq("META")}
    while True:
        response = table().scan(**kwargs)
        for item in response.get("Items", []):
            board_ids.append(item["pk"].split("#", 1)[1])
        token = response.get("LastEvaluatedKey")
        if not token:
            break
        kwargs["ExclusiveStartKey"] = token
    return board_ids


def to_dynamo(value):
    return json.loads(json.dumps(value), parse_float=Decimal)


def from_dynamo(value):
    if isinstance(value, list):
        return [from_dynamo(v) for v in value]
    if isinstance(value, dict):
        return {k: from_dynamo(v) for k, v in value.items()}
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    return value


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


def handle_sqs(event):
    for record in event.get("Records", []):
        board_id = None
        try:
            board_id = json.loads(record.get("body", "{}")).get("board_id")
            confront_board(board_id)
        except Exception:
            logger.exception("Weekly confront failed for board %s", board_id)
            raise
    return {"ok": True}


def confront_board(board_id):
    board = load_board(board_id)
    if board is None:
        return
    weeks = board.get("weeks", [])
    if not weeks:
        return
    history = history_stats(board.get("analyses", []))
    result = analyze_weeks(weeks, history)
    put_analysis(board_id, result, "weekly")


def run_weekly_sweep():
    board_ids = list_active_boards()
    for board_id in board_ids:
        send_to_queue(board_id)
    logger.info("Weekly sweep queued %d boards", len(board_ids))
    return {"queued": len(board_ids)}


def send_to_queue(board_id):
    import boto3

    queue_url = os.environ.get("QUEUE_URL", "")
    if not queue_url:
        return
    boto3.client("sqs", region_name="us-east-1").send_message(
        QueueUrl=queue_url, MessageBody=json.dumps({"board_id": board_id})
    )


def json_response(status, payload):
    headers = dict(CORS_HEADERS)
    headers.update(SECURITY_HEADERS)
    headers["Content-Type"] = "application/json"
    return {
        "statusCode": status,
        "headers": headers,
        "body": json.dumps(payload, default=_json_default),
    }


def _json_default(value):
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    raise TypeError(repr(value))


class AccessDenied(Exception):
    pass
