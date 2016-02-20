# hugo-lambda-function

AWS Lambda function to build a Hugo website. Inspired by Ryan Brown's [hugo-lambda](https://github.com/ryansb/hugo-lambda) and Jeremy Olexa's [hugo-lambda-function](https://github.com/jolexa/hugo-lambda-function)

Also see the blog article [Dynamic GitHub Actions with AWS Lambda](https://aws.amazon.com/de/blogs/compute/dynamic-github-actions-with-aws-lambda/) for detailed information about how to set up GitHub and Lambda.


## Files

`lambda_function.py` - The actual function that gets ran

`build-package.sh` - Helper information to build the zip file


## Ideology

The idea of this repo is to build a zip package that can be deployed to AWS Lambda. This is what happens when everything is in place:

1. A new commit is pushed to GitHub.
2. The GitHub webhook notifies Amazon SNS.
3. The Lambda function fires on the SNS event.
4. The Lambda function creates a 'pending' status for the commit. 
5. It downloads the commit as zip file from GitHub. Then it uses Hugo to generate the static website and syncs it to S3.
6. After a successful deployment, Lambda sets the GitHub commit status to 'success'.

There is also an option for [scheduled events](http://docs.aws.amazon.com/lambda/latest/dg/with-scheduled-events.html) to trigger the build instead of GitHub pushes.


## Usage

Note: For the first website you need to follow all the steps below. For any additional website, only the webhook of the GitHub repo (step 3) and the new AWS S3 bucket (Step 9) must be created. And, if used, a new scheduled event.


### Step 1: Create an SNS Topic

1. Go to the AWS SNS console.
2. Click "Create topic".
3. Fill in any topic name you like and create the topic.
4. Keep the topic ARN around. We'll use that later.


### Step 2: Create an IAM User and a Role

#### IAM User

We need a user for GitHub to publish:

1. Go to the Amazon IAM console.
2. Click "Users" then "Create New Users".
3. Enter a name for the GitHub publisher user. Make sure "Generate an access key for each user" is activated.
4. Create the user.
5. Show the user security credentials and keep them around for later use.
6. Return to the main IAM console page.
7. Click "Users", then click the name of your newly created user to edit its properties.
8. Scroll down to "Permissions" and create a new "Inline Policy".
9. Select the "Custom Policy" radio button, then press "Select".
10. Type a name for your policy, then paste the following statements. They authorize publication to the SNS topic you created before. Here you'll use the topic ARN.

```
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Action": [
        "sns:Publish"
      ],
      "Resource": [
        "<SNS topic ARN goes here>"
      ],
      "Effect": "Allow"
    }
  ]
}
```

This IAM user represents the GitHub publishing process. The policy ensures that this user is only able to publish to the topic we just made. We'll share this user’s credentials with GitHub in a later step. As a security best practice, you should create a unique user for each system that you provide access to, rather than sharing user credentials, and you should always scope access to the minimum set of resources required (in this case, the SNS topic).


#### IAM Role

We also need a role to grant permissions to the Lambda function we will create later.

1. Still in IAM, select "Roles".
2. Click "Create New Role" and give it a name.
3. Select the "AWS Lambda" service role.
4. Do not attach a policy, just click "Next Step", then "Create Role".
5. Open your newly created role.
6. Add an "Inline Policy" and select "Custom Policy".
7. Give it a name and paste the following policy as the policy document and save:

```
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "lambda:InvokeFunction",
                "lambda:GetFunctionConfiguration",
                "lambda:GetFunction"
            ],
            "Resource": [
                "*"
            ]
        },
        {
            "Effect": "Allow",
            "Action": [
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents"
            ],
            "Resource": "arn:aws:logs:*:*:*"
        },
        {
            "Effect": "Allow",
            "Action": "s3:*",
            "Resource": "arn:aws:s3:::*"
        },
        {
            "Effect": "Allow",
            "Action": "dynamodb:*",
            "Resource": "arn:aws:dynamodb:*:*:table/lambdaLocks"
        }
    ]
}
```

With this policy, your Lambda function will have access to AWS S3 and DynamoDB and also allow inspection of a Lambda function and logging. 


### Step 3: Set up the GitHub Webhook

1. Navigate to your GitHub repo.
2. Click on "Settings" in the sidebar.
3. Click on "Webhooks & Services".
4. Click the "Add service" dropdown, then click "AmazonSNS". Fill out the form (supplying the IAM user credentials you created in Step 2), then click "Add service". (Note that the label says "topic", but it requires the entire ARN, not just the topic name.)

Now GitHub actions will publish to your SNS topic.


### Step 4: Build the Lambda Zip Package

1. Open `build-package.sh` and adjust the hugo version.
2. Run `build-package.sh`. This will download Hugo and s3cmd (for syncing with AWS S3) and bundle it up together with `main.py`.

Hint: It's easiest to run the `build-package.sh` script on a Linux machine. If you don't have one you could simple use an online IDE like [Cloud9](https://c9.io/). Just upload the `build-package.sh` and `main.py`, open the `build-package.sh` script and run it.


### Step 5: Create the Lambda Function

1. Open the AWS Lambda console.
2. Click on "Create a Lambda function".
3. Don't choose a blueprint and click on "Skip".
4. Edit function name.
5. Choose "Python 2.7" as runtime.
6. Use the "Upload a .ZIP file" version and select the previousely generated Zip package.
7. As "Role" select the role you created in Step 2.
8. Choose some higher value for "Memory", e.g. 1024.
9. The "Timeout" should be set to the maximum of 5 min.
10. Create the fuction.


### Step 6: Add SNS Event Sources

1. Open your newly created Lambda function and go to "Event sources".
2. Select "Add an Event Source".
3. Select "SNS" and the SNS topic created in Step 1.


### Step 7: Add GitHub Access Token

The Lambda function needs access to our GitHub repository to add commit statuses and commit messages.

1. Open your GitHub account's "Personal access tokens" settings: https://github.com/settings/tokens
2. Click on "Generate new token".
3. Give it a name.
4. Activate the "repo" scope.
5. Generate the token and copy it.
6. Go back to your Lambda function and open the "Configuration" tab.
7. In the "Description" field, add the following JSON including the token:

```
{"github_token": "Your GitHub Access Token goes here"}
```


### Step 8: Create DynamoDB Table

To coordinate concurrently running Lambda functions, our function needs a simple table in DynamoDB for locking.

1. Open the AWS DynamoDB console.
2. Click on "Create table".
3. Enter "lambdaLocks" as table name (this name must be entered exactly like this).
4. Enter "id" as the primary key String.
5. Click "Create".


### Step 9: Create the S3 Bucket

1. Opent the AWS S3 console.
2. Click "Create Bucket".
3. Choose a "Bucket Name".   
**Important:** The bucket name must be the same as the GitHub repository name. And for S3 to work as a static website host the name must be the same as the domain.
4. The "Region" should preferably be the same as the location of the Lambda function (for better sync performance).


That's it. Now run some tests by commiting to your GitHub repository.


### (Optional) Add Scheduled Event Source

If you want to run the Lambda function not just on a GitHub push but also schedule it periodically, you can add another "Event Source" as follows:

1. Open the AWS CloudWatch console.
2. Select "Events"
3. Click the "Create rule" button.
4. Select "Schedule" as "Event selector" and either choose a fixed rate or a cron expression.
5. As "Targets" select "Lambda function" and choose your function.
6. Expand "Configure input".
7. Select "Constant (JSON text)".
8. In the "Constant" field add the following JSON. Adjust "owner" and "repo". You can also change the "ref" if you want to use something else than the master branch:

```
{"event_type": "scheduled", "owner": "Your GitHub username goes here", "repo": "Your GitHub repo name goes here", "ref": "heads/master"}
```


## Future

* [ ] It might be nice to package up the zip file generation in a CloudFormation Stack
* [ ] It would be very nice to package up the entire function/SNS topic in a CloudFormation Stack as well