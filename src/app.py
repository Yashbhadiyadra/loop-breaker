import json
import logging
import os
import re

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

PRIMARY_MODEL = "us.amazon.nova-micro-v1:0"
FALLBACK_MODEL = "amazon.nova-micro-v1:0"
MAX_TASKS = 200

_here = os.path.dirname(__file__)
with open(os.path.join(_here, "index.html"), encoding="utf-8") as f:
    INDEX_HTML = f.read()

bedrock = boto3.client("bedrock-runtime", region_name="us-east-1")

SYSTEM_PROMPT = (
    "You analyze a person's weekly to-do lists to find tasks they keep putting off. "
    "Identify tasks that recur across two or more of the provided weeks. A task recurs "
    "when the same intent appears in different weeks even if the wording differs slightly. "
    "This is the whole point: only a task that shows up in at least two separate weeks "
    "counts. Ignore any task that appears in only one week, no matter how important it "
    "sounds. weeks_seen is the number of distinct weeks the task appears in, and it must be "
    "2 or greater for every task you include. If no task appears in two or more weeks, "
    "return an empty recurring array and say so in the summary. Do not invent recurrence. "
    "For each recurring task, decide a "
    "verdict: COMMIT when it matters and needs a concrete first step now, SCHEDULE when it "
    "should be pinned to a specific time, KILL when it keeps sliding because it does not "
    "actually matter. Be blunt and honest, not encouraging. No motivational fluff. "
    "Return one JSON object with this exact shape: "
    '{"recurring": [{"task": string, "weeks_seen": int, '
    '"verdict": "COMMIT" | "SCHEDULE" | "KILL", "reasoning": string, "next_step": string}], '
    '"summary": string}. '
    "reasoning is one blunt sentence. next_step is a concrete action, or an empty string "
    "when the verdict is KILL. summary is one line about the overall pattern. "
    "Return only valid JSON, no prose before or after."
)

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "content-type",
}


def lambda_handler(event, context):
    request = event.get("requestContext", {}).get("http", {})
    method = request.get("method", "")
    path = event.get("rawPath", "/")

    if method == "OPTIONS":
        return {"statusCode": 204, "headers": CORS_HEADERS, "body": ""}

    if method in ("GET", "HEAD") and path == "/":
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "text/html; charset=utf-8"},
            "body": INDEX_HTML,
        }

    if method == "POST" and path == "/analyze":
        return handle_analyze(event)

    return json_response(404, {"error": "not found"})


def handle_analyze(event):
    try:
        weeks = parse_weeks(event.get("body"))
    except ValueError:
        return json_response(400, {"error": "invalid input"})

    prompt = build_prompt(weeks)

    try:
        raw = invoke_model(prompt)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "AccessDeniedException":
            logger.error("Bedrock access denied: %s", exc)
        else:
            logger.exception("Bedrock invocation failed")
        return json_response(502, {"error": "analysis unavailable"})
    except Exception:
        logger.exception("Bedrock invocation failed")
        return json_response(502, {"error": "analysis unavailable"})

    parsed = extract_json(raw)
    if parsed is None:
        logger.error("Could not parse model output: %s", raw)
        return json_response(502, {"error": "analysis unavailable"})

    return json_response(200, sanitize(parsed))


def sanitize(parsed):
    if not isinstance(parsed, dict):
        return {"recurring": [], "summary": ""}

    items = parsed.get("recurring")
    kept = []
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and weeks_seen(item) >= 2:
                kept.append(item)

    summary = parsed.get("summary", "")
    if not kept:
        summary = "No task showed up in two or more weeks. Nothing is looping yet."

    return {"recurring": kept, "summary": summary if isinstance(summary, str) else ""}


def weeks_seen(item):
    try:
        return int(item.get("weeks_seen", 0))
    except (TypeError, ValueError):
        return 0


def parse_weeks(body):
    if not body:
        raise ValueError("empty body")
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        raise ValueError("not json")

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
        total_tasks += len(tasks)

    if total_tasks > MAX_TASKS:
        raise ValueError("too many tasks")

    return weeks


def build_prompt(weeks):
    lines = []
    for week in weeks:
        lines.append(week["label"])
        for task in week["tasks"]:
            clean = task.strip()
            if clean:
                lines.append("- " + clean)
        lines.append("")
    return "Here are my recent weekly to-do lists.\n\n" + "\n".join(lines)


def invoke_model(prompt):
    body = json.dumps(
        {
            "messages": [{"role": "user", "content": [{"text": prompt}]}],
            "system": [{"text": SYSTEM_PROMPT}],
            "inferenceConfig": {"maxTokens": 800, "temperature": 0.3},
        }
    )
    try:
        response = bedrock.invoke_model(modelId=PRIMARY_MODEL, body=body)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ValidationException":
            response = bedrock.invoke_model(modelId=FALLBACK_MODEL, body=body)
        else:
            raise

    payload = json.loads(response["body"].read())
    return payload["output"]["message"]["content"][0]["text"]


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


def json_response(status, payload):
    headers = dict(CORS_HEADERS)
    headers["Content-Type"] = "application/json"
    return {"statusCode": status, "headers": headers, "body": json.dumps(payload)}
