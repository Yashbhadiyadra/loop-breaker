# Loop Breaker

I kept noticing the same three or four tasks sliding from one weekly to-do list to the next. Not because they were hard, but because they were vague, mildly unpleasant, or things I secretly did not intend to do. Loop Breaker is the tool I built to call that out. You keep a running board of your weekly lists, and it finds the tasks that keep reappearing and forces a decision on each one: commit with a concrete first step, schedule it to a specific time, or kill it honestly. It does not congratulate you for anything. A task that shows up in three straight weeks is not progress, it is a loop, and the point is to break it.

## How it works

The work is split deliberately, because the interesting part of this kind of app is the boundary between what code should do and what a model should do.

The Lambda decides what actually recurs. It normalizes each task, groups tasks that share the same intent across weeks even when the wording differs, and counts how many distinct weeks each group spans. Only groups that appear in two or more weeks survive. This part is deterministic, so the week counts are always right and a one-off task can never be flagged as a loop. If nothing recurs, the function answers immediately without calling the model at all.

When there are real loops, the function hands just those tasks to Amazon Bedrock using the Nova Micro model, which does the one thing a model is good for here: judgment. For each task it returns a verdict, one blunt sentence of reasoning, and a next step, plus a one line summary. It also sees how many earlier reports already flagged each task, so it can get sharper when you have been told the same thing before.

Your board is saved in DynamoDB behind a private link, so it remembers your weeks and every past report. Once a week the board rechecks itself without you. Amazon EventBridge Scheduler wakes up, a planner fans every active board onto an SQS queue, and a worker re-confronts each one and stores a fresh report waiting for your next visit. Failed messages land in a dead-letter queue for replay, so a bad run is recoverable rather than lost. The page itself is still served straight from the Lambda. There is no S3, no API Gateway, and no CloudFront.

## Tests

The recurrence, history, and validation logic is pure Python and covered by unit tests. Run `pip install pytest` and then `pytest` from the repo root.

## Deploy

1. Install the AWS CLI and the AWS SAM CLI, and run `aws configure` with credentials for your account.
2. In the Bedrock console, open Model access, choose Manage model access, check Amazon Nova Micro, and save. Wait until the status reads Access granted.
3. Clone this repo and run `sam build`.
4. Run `sam deploy --guided`. Use stack name `loop-breaker`, region `us-east-1`, and allow SAM to create the IAM roles. SAM provisions the table, the queues, and the weekly schedule for you. The FunctionUrl in the outputs is your live link.

## Cost

Everything here sits inside the AWS free tier at personal volume. DynamoDB runs on 5 read and 5 write capacity units, well under the always free allowance. SQS and EventBridge Scheduler are effectively free at a handful of messages a week. Bedrock is not part of the always free tier, but Nova Micro invocations cost a fraction of a cent, and a board with no recurring tasks never calls the model at all. To tear everything down, run `sam delete --stack-name loop-breaker`.
