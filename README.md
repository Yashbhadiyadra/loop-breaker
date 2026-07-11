# Loop Breaker

I kept noticing the same three or four tasks sliding from one weekly to-do list to the next. Not because they were hard, but because they were vague, mildly unpleasant, or things I secretly did not intend to do. Loop Breaker is the small tool I built to call that out. You paste in your recent weekly lists, and it finds the tasks that keep reappearing and forces a decision on each one: commit with a concrete first step, schedule it to a specific time, or kill it honestly. It does not congratulate you for anything. A task that shows up in three straight weeks is not progress, it is a loop, and the point is to break it.

## How it works

The whole thing is one AWS Lambda function with a public function URL. A GET request returns the single page app as inline HTML, served straight from the function. A POST to /analyze takes your weeks as JSON.

The work is split deliberately. The function itself decides what actually recurs: it normalizes each task, groups tasks that share the same intent across weeks even when the wording differs, and counts how many distinct weeks each group spans. Only groups that appear in two or more weeks survive. This part is deterministic, so the week counts are always right and a one-off task can never be flagged as a loop. If nothing recurs, the function answers immediately without calling the model at all.

When there are real loops, the function hands just those tasks to Amazon Bedrock using the Nova Micro model, which does the one thing a model is good for here: judgment. For each task it returns a verdict, one blunt sentence of reasoning, and a next step, plus a one line summary. There is no database, no S3, no API Gateway. Nothing is stored.

## Deploy

1. Install the AWS CLI and the AWS SAM CLI, and run `aws configure` with credentials for your account.
2. In the Bedrock console, open Model access, choose Manage model access, check Amazon Nova Micro, and save. Wait until the status reads Access granted.
3. Clone this repo and run `sam build`.
4. Run `sam deploy --guided`. Use stack name `loop-breaker`, region `us-east-1`, and allow SAM to create the IAM role. The FunctionUrl in the outputs is your live link.

## Tests

The recurrence and validation logic is pure Python and covered by unit tests. Run `pip install pytest` and then `pytest` from the repo root.

## Cost

Bedrock is not part of the always free tier, but Nova Micro is priced so low that a single analysis costs a fraction of a cent, and analyses with no recurring tasks cost nothing because the model is never called. For personal use you will not notice it. To tear everything down, run `sam delete --stack-name loop-breaker`.
