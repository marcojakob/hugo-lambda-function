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

lambda_client = boto3.client('lambda')

def lambda_handler(event, context):
    # Load the description from the Lambda Function Console. This is
    # a workaround to get some sort of runtime configuration.
    # We use it for the GitHub access token.
    github_token = lambda_client.get_function_configuration(FunctionName=context.function_name)['Description']
  
    #logger.info("Event: " + str(event))
    message = json.loads(event['Records'][0]['Sns']['Message'])
    #logger.info("Message: " + str(message))

    repourl = message['repository']['url']
    reponame = message['repository']['name']
    repofullname = message['repository']['full_name']
    
    downloadUrl = "https://api.github.com/repos/" + repofullname + "/zipball";

    logger.info("Downloading from: " + downloadUrl)

    # Download zip from GitHub with authentication headers. 
    req = urllib2.Request(downloadUrl, headers = { 'Authorization' : 'token ' + github_token })
    
    with open("/tmp/repo.zip", "wb") as f:
        f.write(urllib2.urlopen(req).read())
        
    #urllib.urlretrieve (repourl + "/archive/master.zip", "/tmp/repo.zip")
    zfile = zipfile.ZipFile('/tmp/repo.zip')
    zfile.extractall("/tmp")
    os.remove('/tmp/repo.zip')
    
    # Get the folder name of the extracted repository.
    builddir = '/tmp/' + os.listdir('/tmp')[0] + "/"
    #builddir = "/tmp/" + reponame + "-master/"

    subprocess.call("/var/task/hugo_0.15_linux_amd64.go" , shell=True, cwd=builddir)
    
    pushdir = builddir + "public/"
    bucketuri = "s3://" + reponame + "/"
    
    subprocess.call("python /var/task/s3cmd/s3cmd sync --delete-removed"
        + " --no-mime-magic --no-preserve"
        + " " + pushdir + " " + bucketuri, shell=True)
