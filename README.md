# Loop Breaker

I kept noticing the same three or four tasks sliding from one weekly to-do list to the next. Not because they were hard, but because they were vague, mildly unpleasant, or things I secretly did not intend to do. Loop Breaker is the small tool I built to call that out. You paste in your recent weekly lists, and it finds the tasks that keep reappearing and forces a decision on each one: commit with a concrete first step, schedule it to a specific time, or kill it honestly. It does not congratulate you for anything. A task that shows up in three straight weeks is not progress, it is a loop, and the point is to break it.

## How it works

The whole thing is one AWS Lambda function with a public function URL. A GET request returns the single page app as inline HTML, served straight from the function. A POST to /analyze takes your weeks as JSON, builds a prompt, and calls Amazon Bedrock using the Nova Micro model. Nova Micro is cheap and fast, which is exactly right for a short structured reasoning task like this. The model returns JSON: for each recurring task it gives a verdict, one blunt sentence of reasoning, and a next step, plus a one line summary of the overall pattern. The browser renders that into color coded cards. There is no database, no S3, no API Gateway. Everything lives in the function, and the model does the judgment.

## Deploy

1. Install the AWS CLI and the AWS SAM CLI, and run `aws configure` with credentials for your account.
2. In the Bedrock console, open Model access, choose Manage model access, check Amazon Nova Micro, and save. Wait until the status reads Access granted.
3. Clone this repo and run `sam build`.
4. Run `sam deploy --guided`. Use stack name `loop-breaker`, region `us-east-1`, and allow SAM to create the IAM role. The FunctionUrl in the outputs is your live link.

## Cost

Bedrock is not part of the always free tier, but Nova Micro is priced so low that a single analysis costs a fraction of a cent. For personal use you will not notice it. To tear everything down, run `sam delete --stack-name loop-breaker`.
