from __future__ import print_function

import json
import logging
import boto3
import os
import time
from datetime import datetime, timedelta

import urllib
import urllib2
import zipfile
import subprocess

logger = logging.getLogger()
logger.setLevel(logging.INFO)

lambda_client = boto3.client('lambda')
dynamodb_client = boto3.client('dynamodb')


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
    latest_sha = github.get_latest_sha()
    if not github.sha == latest_sha:
        logger.info('Ignoring event because it does not contain the latest ' + 
                    'commit: event-sha: ' + github.sha + ' latest-sha: ' +
                    latest_sha)
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
        subprocess.check_output('/var/task/hugo.go', shell=True, cwd=builddir + '/', stderr=subprocess.STDOUT)
        hugo_time = time.time() - hugo_time
        
    except subprocess.CalledProcessError as e:
        github.set_status('error', 'Failed to generate site with Hugo')
        github.create_commit_comment(':x: **Failed to generate site with ' + 
                                     'Hugo**\n\n' + e.output)
        raise Exception('Failed to generate site with Hugo: ' + 
                        e.output)

    # 3. Sync to S3.
    pushdir = builddir + '/public/'
    bucketuri = 's3://' + github.repo_name + '/'

    try:
        acquire_lock(github.repo_name)
        logger.info('Syncing to S3 bucket: ' + bucketuri)
        sync_time = time.time()
        subprocess.check_output('python /var/task/s3cmd/s3cmd sync ' + 
                                '--delete-removed --no-mime-magic ' + 
                                '--no-preserve ' + pushdir + ' ' + 
                                bucketuri, shell=True, stderr=subprocess.STDOUT)
        sync_time = time.time() - sync_time
    except subprocess.CalledProcessError as e:
        github.set_status('error', 'Failed to sync generated site to Amazon S3')
        github.create_commit_comment(':x: **Failed to sync generated site to ' +
                                     'Amazon S3**\n\n' + e.output)
        raise Exception('Failed to sync generated site to Amazon S3: ' + 
                        e.output)
    finally:
        release_lock(github.repo_name)

    # 4. Success!
    github.set_status('success', 'Successfully generated and deployed static site')
    total_time = time.time() - total_time
    stats = 'Repo download size: ' + str(download_size / 1000) + ' kilobytes\n' + \
            'Repo download time: ' + '%.3f' % download_time + ' seconds\n' + \
            'Hugo build time: ' + '%.3f' % hugo_time + ' seconds\n' + \
            'S3 sync time: ' + '%.3f' % sync_time + ' seconds\n' + \
            'Total time: ' + '%.3f' % total_time + ' seconds'
    logger.info('Successfully generated and deployed static site\n' + stats)
    github.create_commit_comment(':white_check_mark: **Sucessfully generated ' + 
             'and deployed static site**\n\n' + stats)


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


def acquire_lock(bucket):
    """ Tries to get a write lock for the specified bucket. 
    If the lock was already acquired from another Lambda function, this
    function sleeps and tries again. If the lock could be acquired, the 
    function returns.
    """
    try:
        sleep = 1
        while True:
            lock_item = dynamodb_client.get_item(TableName='lambdaLocks', 
                                                 Key={'bucket':{'S':bucket}},
                                                 ConsistentRead=True)
            
            if 'Item' not in lock_item:
                # No lock item, we acquire it.
                create_lock_item(bucket)
                return
            
            lock_date = datetime.strptime(lock_item['Item']['created']['S'], "%Y-%m-%d %H:%M:%S")

            # Check if the lock item is more than 300s old (the max execution
            # duration of a Lambda function).
            if lock_date + timedelta(seconds=300) < datetime.now():
                # Just overwrite the invalid lock item with our own.
                logger.error('Lock item for bucket "' + bucket + '" was older ' + 
                             'than 300s (' + lock_date + '). A Lambda function ' + 
                             'did not release it. I will overwrite it.')
                create_lock_item(bucket)
                return
            
            logger.info('Waiting to acquire lock. Sleeping for ' + str(sleep) + ' seconds')
            time.sleep(sleep)
            # Double the sleep time for the next iteration.
            sleep = sleep * 2
    except Exception as e:
        logger.warning('Could not acquire lock for writing to S3. Will write anyway ' +
                       'and hope nothing bad happens: ' + str(e))
                            
def create_lock_item(bucket):
    logger.info('Acquired lock item for writing to S3.')
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    dynamodb_client.put_item(TableName='lambdaLocks', 
                             Item={'bucket':{'S':bucket}, 'created':{'S':now}})
                                            
def release_lock(bucket):
    dynamodb_client.delete_item(TableName='lambdaLocks', 
                                Key={'bucket':{'S':bucket}})
    

class GitHubInfo(object):
    """ Class that represents information from GitHub."""

    def __init__(self, message, context):
        """ Returns a new object."""

        self.github_token = self.read_github_token(context)

        self.ref = message['ref']
        self.sha = message['head_commit']['id']
        self.commit_url = message['head_commit']['url']
        self.owner = message['repository']['owner']['name']
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

    def get_latest_sha(self):
        """Returns the latest commit sha of this ref.
        This function makes a request to the GitHub Api.
        """
        url = 'https://api.github.com/repos/' + self.repo_full_name + \
              '/git/' + self.ref
        req = urllib2.Request(url)
        req.add_header('Authorization', 'token ' + self.github_token)
        response = json.load(urllib2.urlopen(req))
        return response['object']['sha']


    def download(self):
        """Downloads the latest commit from GitHub.
        Returns the directory string of the downloaded content.
        """
        
        url = 'https://api.github.com/repos/' + self.repo_full_name + \
              '/zipball/' + self.sha
        req = urllib2.Request(url)
        req.add_header('Authorization', 'token ' + self.github_token)

        logger.info('Download request: ' + url)
        
        # Download zip.
        with open('/tmp/repo.zip', 'wb') as f:
            f.write(urllib2.urlopen(req).read())
            
        download_size = os.path.getsize('/tmp/repo.zip')
            
        # Unzip.
        zfile = zipfile.ZipFile('/tmp/repo.zip')
        zfile.extractall('/tmp')
        os.remove('/tmp/repo.zip')

        # Get the folder name of the extracted repository.
        directory = '/tmp/' + self.owner + '-' + self.repo_name + '-' + self.sha
        #directory = '/tmp/unzipped/' + os.listdir('/tmp/unzipped')[0]

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
