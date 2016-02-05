from __future__ import print_function

import json
import logging
import boto3
import os

import urllib
import urllib2
import zipfile
import subprocess

logger = logging.getLogger()
logger.setLevel(logging.INFO)

lambda_client = boto3.client('lambda')


def lambda_handler(event, context):
    """ The main lambda function."""

    # Init GitHub info and status.
    github = GitHubInfo(event, context)
    github.set_status('pending', 'Generating static site')

    # 1. Download.
    try:
        builddir = github.download()
    except urllib2.URLError as e:
        github.set_status('error', 'Failed to download latest commit ' +
                          'from GitHub')
        raise e

    # 2. Hugo build.
    try:
        logger.info('Running Hugo with build directory: ' + builddir)
        subprocess.call('/var/task/hugo.go', shell=True, cwd=builddir)
    except:
        github.set_status('error', 'Failed to generate website with Hugo.')
        raise

    # 3. Sync to S3.
    try:
        pushdir = builddir + 'public/'
        bucketuri = 's3://' + github.repo_name + '/'

        logger.info('Syncing to S3. Bucket: ' + bucketuri)
        subprocess.call('python /var/task/s3cmd/s3cmd sync --delete-removed' +
                        ' --no-mime-magic --no-preserve' +
                        ' ' + pushdir + ' ' + bucketuri, shell=True)
    except:
        github.set_status('error', 'Failed to sync to S3.')
        raise

    # 4. Success!
    github.set_status('success', 'Successfully generated and deployed ' +
                      'static site.')


class GitHubInfo(object):
    """ Class that represents information from GitHub."""

    def __init__(self, event, context):
        """ Returns a new object."""

        self.github_token = self.read_github_token(context)

        # Parse the GitHub payload message.
        message = json.loads(event['Records'][0]['Sns']['Message'])
        self.commit_ref = message['ref']
        self.head_commit = message['head_commit']['id']
        self.repo_name = message['repository']['name']
        self.repo_full_name = message['repository']['full_name']

    def read_github_token(self, context):
        """ Returns the GitHub access token.
        Searches the description from the Lambda function console for the
        token. This is a workaround to get some sort of runtime configuration.
        """

        lambda_desc = lambda_client.get_function_configuration(
                FunctionName=context.function_name)['Description']

        if lambda_desc.startswith('github_token:'):
            logger.info('Found GitHub access token.')
            return lambda_desc[len('github_token:'):].strip()
        else:
            self.set_status('error', 'No GitHub access token provided.')
            raise Exception('No GitHub access token provided. Set ' +
                            '"github_token:[...]" as the lambda function ' +
                            'description.')

    def download(self):
        """Downloads the latest commit from GitHub.
        Returns the directory string of the downloaded content.
        """
        
        url = 'https://api.github.com/repos/' + self.repo_full_name + \
              '/zipball/' + self.commit_ref
        req = urllib2.Request(url)
        req.add_header('Authorization', 'token ' + self.github_token)

        logger.info('Download request. Commit: ' + self.head_commit +
                    ' Url: ' + url)
        
        # Download zip.
        with open('/tmp/repo.zip', 'wb') as f:
            f.write(urllib2.urlopen(req).read())

        # Unzip.
        zfile = zipfile.ZipFile('/tmp/repo.zip')
        zfile.extractall('/tmp')
        os.remove('/tmp/repo.zip')

        # Get the folder name of the extracted repository.
        return '/tmp/' + os.listdir('/tmp')[0] + '/'

    def set_status(self, state, description=''):
        """ Sets the commit status.
        Possible states are: pending, success, error, or failure.
        """

        payload = {
          'state': state,
          'target_url': 'https://example.com/build/status',
          'description': description,
          'context': 'hugo-lambda'
        }

        # Post status to url.
        url = 'https://api.github.com/repos/' + self.repo_full_name + \
              '/statuses/' + self.head_commit
        req = urllib2.Request(url, json.dumps(payload))
        req.add_header('Authorization', 'token ' + self.github_token)
        req.add_header('Content-Type', 'application/json')
        urllib2.urlopen(req)
        
    def create_deployment(self):
        """ Creates a deployment and returns the deployment id.
        """
        payload = {
          'ref': self.head_commit
        }

        # Post status to url.
        url = 'https://api.github.com/repos/' + self.repo_full_name + \
              '/deployments'
        req = urllib2.Request(url, json.dumps(payload))
        req.add_header('Authorization', 'token ' + self.github_token)
        req.add_header('Content-Type', 'application/json')
        response = json.load(urllib2.urlopen(req))
        return str(response['id'])

    def set_deployment_status(self, deployment_id, state):
        """ Sets the deployment status.
        Possible states are: pending, success, error, or failure.
        """
        payload = {
          'state': state,
          'target_url': 'https://example.com/build/status'
        }

        # Post status to url.
        url = 'https://api.github.com/repos/' + self.repo_full_name + \
              '/deployments/' + deployment_id + '/statuses'
        req = urllib2.Request(url, json.dumps(payload))
        req.add_header('Authorization', 'token ' + self.github_token)
        req.add_header('Content-Type', 'application/json')
        urllib2.urlopen(req)