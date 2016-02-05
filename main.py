from __future__ import print_function

import json
import logging
import boto3
import os
import time

import urllib
import urllib2
import zipfile
import subprocess

logger = logging.getLogger()
logger.setLevel(logging.INFO)

lambda_client = boto3.client('lambda')


def lambda_handler(event, context):
    """ The main lambda function."""

    total_time = time.time()
    
    sns = event['Records'][0]['Sns']
    message = json.loads(sns['Message'])
    
    # Ignore some events.
    if not valid_event(sns, message):
        return
    
    # Init GitHub info.
    github = GitHubInfo(message, context)
    
    # Ignore if it isn't the latest commit.
    # When an exception was thrown, Lambda automatically does two retries. 
    # This can lead to a situation where the retry does not contain the 
    # most recent commit.
    if not github.is_latest_commit():
        logger.info('Ignoring event because it does not contain the latest ' + 
                    'commit.')
        return
    
    # We can start.
    github.set_status('pending', 'Generating static site')

    
    # 1. Download.
    try:
        download_time = time.time()
        builddir, download_size = github.download()
        download_time = time.time() - download_time
    except urllib2.URLError as e:
        github.set_status('error', 'Failed to download latest commit ' +
                          'from GitHub')
        raise e

    # 2. Hugo build.
    logger.info('Running Hugo in build directory: ' + builddir)
    try:
        hugo_time = time.time()
        subprocess.check_output('/var/task/hugo.go', shell=True, cwd=builddir, stderr=subprocess.STDOUT)
        hugo_time = time.time() - hugo_time
        
    except subprocess.CalledProcessError as e:
        github.set_status('error', 'Failed to generate website with Hugo')
        github.create_commit_comment(':x: **Failed to generate website with ' + 
                                     'Hugo**\n\n' + e.output)
        raise Exception('Failed to generate website with Hugo: ' + 
                        e.output)

    # 3. Sync to S3.
    pushdir = builddir + 'public/'
    bucketuri = 's3://' + github.repo_name + '/'

    logger.info('Syncing to S3 bucket: ' + bucketuri)
    try:
        sync_time = time.time()
        subprocess.check_output('python /var/task/s3cmd/s3cmd sync ' + 
                                '--delete-removed --no-mime-magic ' + 
                                '--no-preserve ' + pushdir + ' ' + 
                                bucketuri, shell=True, stderr=subprocess.STDOUT)
        sync_time = time.time() - sync_time
    except subprocess.CalledProcessError as e:
        github.set_status('error', 'Failed to sync generated website to Amazon S3')
        github.create_commit_comment(':x: **Failed to sync generated website to ' +
                                     'Amazon S3**\n\n' + e.output)
        raise Exception('Failed to sync generated website to Amazon S3: ' + 
                        e.output)

    # 4. Success!
    github.set_status('success', 'Successfully generated and deployed static website')
    total_time = time.time() - total_time
    stats = 'Repo download size: ' + str(download_size / 1000) + ' kilobytes\n' + \
            'Repo download time: ' + '%.3f' % download_time + ' seconds\n' + \
            'Hugo build time: ' + '%.3f' % hugo_time + ' seconds\n' + \
            'S3 sync time: ' + '%.3f' % sync_time + ' seconds\n' + \
            'Total time: ' + '%.3f' % total_time + ' seconds'
    logger.info('Successfully generated and deployed static website\n' + stats)
    github.create_commit_comment(':white_check_mark: **Sucessfully generated ' + 
             'and deployed static website**\n\n' + stats)


def valid_event(sns, message):
    """Tests if the event is a valid push event on the default branch"""
    github_event = sns['MessageAttributes']['X-Github-Event']['Value']
    
    # Ignore non 'push' events.
    if not github_event == 'push':
        logger.info('Ignoring github event: ' + github_event)
        return False

    # Ignore newly created or deleted branches.
    if message['created']:
        logger.info('Ignoring branch "created" event.')
        return False
    if message['deleted']:
        logger.info('Ignoring branch "deleted" event.')
        return False
    
    # Ignore non-default branches.
    ref = message['ref']
    branch = ref[ref.rfind('/') + 1:]
    default_branch = message['repository']['default_branch']
    if not branch == default_branch:
        logger.info('Ignoring events of non-default branch: ' +
                    branch + ', default branch: ' + default_branch)
        # Stop lambda function.
        return False
    
    # Valid event.
    logger.info('Received a push event on default branch: ' + 
                branch + ' (' + ref + ')')
    return True


class GitHubInfo(object):
    """ Class that represents information from GitHub."""

    def __init__(self, message, context):
        """ Returns a new object."""

        self.github_token = self.read_github_token(context)

        self.ref = message['ref']
        self.sha = message['head_commit']['id']
        self.commit_url = message['head_commit']['url']
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
            self.set_status('error', 'No GitHub access token provided')
            raise Exception('No GitHub access token provided. Set ' +
                            '"github_token:[...]" as the lambda function ' +
                            'description.')
            
    def is_latest_commit(self):
        """Returns true if it is the latest commit of this ref"""
        url = 'https://api.github.com/repos/' + self.repo_full_name + \
              '/git/' + self.ref
        req = urllib2.Request(url)
        req.add_header('Authorization', 'token ' + self.github_token)
        response = json.load(urllib2.urlopen(req))
        return response['object']['sha'] == self.sha


    def download(self):
        """Downloads the latest commit from GitHub.
        Returns the directory string of the downloaded content.
        """
        
        url = 'https://api.github.com/repos/' + self.repo_full_name + \
              '/zipball/' + self.ref
        req = urllib2.Request(url)
        req.add_header('Authorization', 'token ' + self.github_token)

        logger.info('Download request: ' + url)
        
        # Download zip.
        with open('/tmp/repo.zip', 'wb') as f:
            f.write(urllib2.urlopen(req).read())
            
        download_size = os.path.getsize('/tmp/repo.zip')
            
        # Unzip.
        zfile = zipfile.ZipFile('/tmp/repo.zip')
        zfile.extractall('/tmp/unzipped')
        os.remove('/tmp/repo.zip')

        # Get the folder name of the extracted repository.
        directory = '/tmp/unzipped/' + os.listdir('/tmp/unzipped')[0] + '/'
        return (directory, download_size)
                

    def set_status(self, state, description=''):
        """ Sets the commit status.
        Possible states are: pending, success, error, or failure.
        """

        logger.info('Setting status "' + state + '" to commit ' + self.sha)
        
        payload = {
          'state': state,
          'target_url': self.commit_url,
          'description': description,
          'context': 'hugo-lambda'
        }

        # Post status to url.
        url = 'https://api.github.com/repos/' + self.repo_full_name + \
              '/statuses/' + self.sha
        req = urllib2.Request(url, json.dumps(payload))
        req.add_header('Authorization', 'token ' + self.github_token)
        req.add_header('Content-Type', 'application/json')
        urllib2.urlopen(req)
        
    def create_deployment(self):
        """ Creates a deployment and returns the deployment id.
        """
        payload = {
          'ref': self.sha
        }

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

        url = 'https://api.github.com/repos/' + self.repo_full_name + \
              '/deployments/' + deployment_id + '/statuses'
        req = urllib2.Request(url, json.dumps(payload))
        req.add_header('Authorization', 'token ' + self.github_token)
        req.add_header('Content-Type', 'application/json')
        urllib2.urlopen(req)
        
    def create_commit_comment(self, comment):
        """ Creates a commit message."""
        
        payload = {
          'body': comment
        }

        url = 'https://api.github.com/repos/' + self.repo_full_name + \
              '/commits/' + self.sha + '/comments'
        req = urllib2.Request(url, json.dumps(payload))
        req.add_header('Authorization', 'token ' + self.github_token)
        req.add_header('Content-Type', 'application/json')
        urllib2.urlopen(req)
