from __future__ import print_function

import json
import logging
import boto3
import os

import urllib2
import zipfile
import subprocess

logger = logging.getLogger()
logger.setLevel(logging.INFO)

lambdaClient = boto3.client('lambda')

def lambda_handler(event, context):
    # Load the description from the Lambda Function Console. This is
    # a workaround to get some sort of runtime configuration.
    # We use it for the GitHub access token.
    lambdaDescription = lambdaClient.get_function_configuration(FunctionName=context.function_name)['Description']
    githubToken = None
    if lambdaDescription.startswith('github_token:'):
        logger.info('Found GitHub access token in lambda function description.')
        githubToken = lambdaDescription[len('github_token:'):].strip()
    else:
        logger.warning('No GitHub access token provided. Only public repos will be accessible. \
                        For private repos set "github_token:[...]" as the lambda function description.')
  
    #logger.info("Event: " + str(event))
    message = json.loads(event['Records'][0]['Sns']['Message'])
    #logger.info("Message: " + str(message))

    commitRef = message['ref']
    headCommit = message['head_commit']['id']
    repoUrl = message['repository']['url']
    repoName = message['repository']['name']
    repoFullName = message['repository']['full_name']
    
    downloadUrl = "https://api.github.com/repos/" + repoFullName + "/zipball/" + commitRef;

    logger.info("Downloading commit " + headCommit + " from: " + downloadUrl)

    # Download zip from GitHub optionally with provided authentication headers for private repos.
    req = urllib2.Request(downloadUrl)
    if githubToken is not None:
        req.add_header('Authorization', 'token ' + githubToken)        
    
    with open("/tmp/repo.zip", "wb") as f:
        f.write(urllib2.urlopen(req).read())
        
    zfile = zipfile.ZipFile('/tmp/repo.zip')
    zfile.extractall("/tmp")
    os.remove('/tmp/repo.zip')
    
    # Get the folder name of the extracted repository.
    builddir = '/tmp/' + os.listdir('/tmp')[0] + "/"
    logger.info('Running Hugo in Build directory: ' + builddir)

    subprocess.call("/var/task/hugo.go" , shell=True, cwd=builddir)
    
    pushdir = builddir + "public/"
    bucketuri = "s3://" + repoName + "/"
    
    subprocess.call("python /var/task/s3cmd/s3cmd sync --delete-removed"
        + " --no-mime-magic --no-preserve"
        + " " + pushdir + " " + bucketuri, shell=True)
