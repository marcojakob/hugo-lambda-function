# hugo-lambda-function

AWS Lambda function to build a Hugo website. Inspired by Ryan Brown's [hugo-lambda](https://github.com/ryansb/hugo-lambda) and Jeremy Olexa's [hugo-lambda-function](https://github.com/jolexa/hugo-lambda-function)


## Files

`main.py` - The actual function that gets ran

`build-package.sh` - Helper information to build the zip file


## Ideology

The idea of this repo is to build a zip package that can be deployed to AWS Lambda. This is what happens when everything is in place:

1. A new commit is pushed to GitHub.
2. The GitHub webhook notifies Amazon SNS.
3. The lambda function fires on the SNS event.
4. The lambda function creates a 'pending' status for the commit. 
5. It downloads the commit as zip file from GitHub. Then it uses Hugo to generate the static website and syncs it to S3.
6. After a successful deployment, lambda sets the GitHub commit status to 'success'.


## Usage

1. Publish the site contents repo to a AWS SNS Topic. In GitHub, repo settings -> Webhooks
  * Recommended: IAM user for the credentials in GitHub.
2. Run `build-package.sh` and use the generated zip file to create a new AWS Lambda function. Subscribe the Lambda function to the SNS topic. (No special IAM permissions are needed for this).
3. Lambda function's job is to build the static content and push to a S3 bucket of the same name as the repo name.
  * Lambda function will need to have IAM permissions to read/list/put/delete S3 bucket objects and Cloudwatch Logging permissions.
 

## Future

* [ ] It might be nice to package up the zip file generation in a CloudFormation Stack
* [ ] It would be very nice to package up the entire function/SNS topic in a CloudFormation Stack as well
* [ ] Fine-tune `main.py` to not act on all GitHub events but just the push on master.