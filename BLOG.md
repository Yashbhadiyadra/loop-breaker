# Weekend Productivity Challenge: Loop Breaker

Loop Breaker is a serverless app that finds the tasks you keep copying from one weekly to-do list to the next, then makes you decide on each one: commit, schedule, or kill. It runs entirely on AWS. Amplify hosts the frontend, a single Lambda behind a function URL runs the backend, DynamoDB stores the boards, SQS and EventBridge Scheduler drive a weekly re-check, Amazon SES sends the results by email, and Amazon Bedrock does the judging with the Nova Micro and Nova Lite models.

## Vision & What the App Does

I have tried a lot of to-do systems, and they all failed me the same way. Every Sunday I would write a fresh weekly list, and every week three or four tasks would quietly walk across from the last one. "Finish thesis chapter 3." "Back up the old laptop." "Cancel the gym membership." They were never hard. They were vague, mildly unpleasant, or things I secretly did not intend to do. A normal to-do app is happy to let you carry a task forever. It never asks the obvious question: if this has been on your list for three weeks, what is actually going on?

Loop Breaker is my answer to that question. You keep a running board of your recent weekly lists. It looks across the weeks, finds the tasks that keep reappearing, and refuses to let them sit there unnamed. For each recurring task it gives you a verdict and a reason:

- COMMIT when the task matters and the real blocker is that it is vague or daunting, so it needs a concrete first step today.
- SCHEDULE when the task matters but keeps sliding because it has no fixed time, so it needs to be pinned to a slot.
- KILL when the task does not actually matter enough to justify the space it keeps taking, and the honest move is to let it go.

It does not stop at a label. Every card also carries a priority, a short line naming why that specific task keeps slipping, a rough effort estimate, and a first step you can act on. A small strip shows which weeks the task appeared in, so a three week loop looks different from a two week one at a glance. When a task was already flagged in an earlier report, the tone gets sharper, because being reminded gently for the third time is part of how these tasks survive.

Two things make it feel less like a form and more like something watching your back. First, you do not have to type your lists. You can point your camera at a handwritten page or a screenshot and it reads the tasks straight off the image. Second, the board does not wait for you to come back. Once a week it re-checks itself and, if you left an email, it sends you the verdict without you lifting a finger.

![Loop Breaker start screen](screenshots/01-empty.png)

## AWS Services Used / Architecture Overview

I wanted the smallest possible surface. No API Gateway, no S3 buckets for the site, no containers. Here is the shape of it:

```
Browser (AWS Amplify hosting)
   |  HTTPS (JSON)
   v
AWS Lambda  (function URL)
   |  reads/writes            |  judges           |  reads photos
   v                          v                   v
Amazon DynamoDB        Bedrock Nova Micro    Bedrock Nova Lite

Weekly, on a schedule:
EventBridge Scheduler -> Lambda (planner) -> SQS -> Lambda (worker)
                                              |         |
                                    dead-letter queue   -> Amazon SES (email)
```

- **AWS Amplify** hosts the single page frontend and redeploys it on every push to the main branch of the repo.
- **AWS Lambda**, reached through a function URL, is the whole backend. The same function serves the JSON API, acts as the SQS worker, and is the target of the weekly schedule. It decides which job it is running by looking at the shape of the event it receives.
- **Amazon DynamoDB** is a single table that stores each board and its history behind an unguessable link id, with a time to live on old reports so they age out on their own.
- **Amazon SQS** carries one message per board during the weekly sweep, with a dead-letter queue so a failed run can be replayed instead of lost.
- **Amazon EventBridge Scheduler** wakes the app up once a week to start that sweep.
- **Amazon SES** sends the weekly confrontation email.
- **Amazon Bedrock** does the reasoning. Nova Micro handles the verdicts because it is fast and cheap enough to call on every review, and Nova Lite, which is multimodal, reads the photos of to-do lists.

## How I Built It

I set one rule at the start and let it shape everything: the code decides what is wrong, and the model only explains it.

The part that decides what actually recurs is plain Python, and it never touches AWS. It normalizes each task, groups tasks that mean the same thing across weeks even when the wording drifts ("email professor" and "email professor about defense date" are the same loop), and counts how many distinct weeks each group spans. Only groups that appear in two or more weeks survive. Because this is deterministic, the week counts are always right, a one-off task can never be mislabeled as a loop, and I could cover the whole thing with unit tests that run in a tenth of a second. If nothing recurs, the app answers instantly and never calls a model at all.

Only then does Bedrock enter. Nova Micro gets the tasks that already passed the recurrence check, along with how many earlier reports flagged each one, and its job is judgment: the verdict, the reasoning, the priority, and the next step. It is not allowed to invent tasks or change the counts. Anything it adds that does not match a task the code already found gets dropped before it reaches the screen, and if the model times out or returns something that is not valid JSON, the deterministic result is still there to fall back on.

![Filling in three weeks of lists](screenshots/02-input.png)

The autonomous weekly check was the piece I most wanted to get right, because it is what separates a tool you have to remember from one that remembers you. A schedule fires once a week and runs the app in "planner" mode, where it lists every active board and drops one message per board onto an SQS queue. Each message triggers the same Lambda in "worker" mode, which re-confronts that board, stores a fresh report, and emails it out. Fanning the work through a queue keeps each run short and cheap, and the dead-letter queue means one bad board does not sink the whole sweep.

A few things fought back along the way.

The first was the Lambda function URL. It wraps the incoming HTTP request in a stringified body, so my first attempts to read the posted JSON came back empty until I parsed the body envelope myself. This is a small thing that costs you an hour the first time you meet it.

The second was more interesting and only showed up once I moved the frontend onto Amplify. Suddenly every request from the site failed. The browser was rejecting the response because the `Access-Control-Allow-Origin` header had two values in it. It turned out both the function URL's own CORS configuration and my Lambda code were each adding the header, and same-origin testing had hidden the duplicate completely, because a browser only checks CORS when the request is cross-origin. The fix was to pick one owner for CORS. I let the application code handle it and removed the configuration from the function URL. I would not have caught this without driving the live site from the Amplify domain, which is a good argument for testing the real thing rather than the local copy.

The third was keeping Nova Micro honest. Early on it liked to reach for KILL whenever a task repeated, as if repetition were the crime. Most recurring tasks are not junk, they are just stuck, so I rewrote the instructions to treat KILL as the rare case and to lean toward COMMIT or SCHEDULE, and to get sharper, not softer, when it sees a task it has already flagged before.

![The verdicts, with priority, effort, and a loop history strip](screenshots/03-results.png)

## What I Learned

Putting deterministic code in front of the model was the decision that made the project work. The findings are trustworthy because they come from code I can test, not from output I have to second-guess, and the layer that matters never depends on the network being up. That pattern, let the code decide and let the model explain, is the thing I will carry into the next build.

Nova Micro surprised me on price and speed. For short, structured judgment work like this, a small specialized model is not a compromise, it is the right tool, and it costs a fraction of a cent per review. Nova Lite reading a photo of a scribbled list on the first try was the moment the app stopped feeling like a form.

I also got a real feel for decoupling. It would have been easy to loop over every board inside one Lambda during the weekly sweep, and it would have worked until it did not. Pushing each board through SQS with a dead-letter queue turned a fragile batch job into something that stays short, retries on its own, and never loses a board.

The last lesson was about which feature actually mattered. Everything up to that point produced a report you had to come back and read. The weekly email was small to add, but it is the thing that turns Loop Breaker from a page you visit into something that shows up and confronts you on its own. That is the whole point of the app, and it only existed once the app could act without me.

## Link to App or Repo

Live app: https://main.d30dqy7101ufij.amplifyapp.com/

Source code: https://github.com/Yashbhadiyadra/loop-breaker

#productivity #challenge #aws-amplify #amazon-bedrock #amazon-nova #amazon-dynamodb #aws-lambda #serverless
