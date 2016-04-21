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

    start_datetime = datetime.utcnow()
    total_time = time.time()

    # 1. Init GitHub.
    try:
        github = GitHub(event, context)
    except EventIgnoreException as e:
        # We ignore the event, for example a push to a non-default branch on GitHub.
        logger.info(e.msg)
        return
    except EventInvalidException as e:
        # Invalid event.
        logger.error(e.msg)
        return

    # We can start.
    github.set_status('pending', 'Generating static site')


    # 2. Download.
    try:
        download_time = time.time()
        builddir, download_size = github.download()
        download_time = time.time() - download_time
    except urllib2.URLError as e:
        github.set_status('error', 'Failed to download latest commit ' +
                          'from GitHub')
        raise e


    # 3. Hugo build.
    logger.info('Running Hugo in build directory: ' + builddir)
    try:
        hugo_time = time.time()

        cmd = '/var/task/hugo.go'
        if github.draft:
            cmd += ' --buildDrafts --buildFuture'
        h_out = subprocess.check_output(cmd, shell=True, cwd=builddir + '/',
                                       stderr=subprocess.STDOUT)
        logger.info('Hugo output:\n' + h_out)
        hugo_time = time.time() - hugo_time

    except subprocess.CalledProcessError as e:
        github.set_status('error', 'Failed to generate site with Hugo')
        github.create_commit_comment(':x: **Failed to generate site with ' +
                                     'Hugo**\n\n' + e.output)
        raise Exception('Failed to generate site with Hugo: ' +
                        e.output)


    # 4. Sync to S3.
    pushdir = builddir + '/public/'
    bucketuri = 's3://'
    repo = github.repo
    if github.draft:
        bucketuri += 'draft.'

        # Create a robots file so search engines ingore draft websites.
        with open(pushdir + 'robots.txt', 'w+') as f:
            # w+ overwrites the existing file if the file exists.
            f.write('User-agent: *\nDisallow: /')

        if repo.startswith('www.'):
            repo = repo[4:]

    bucketuri += repo + '/'

    try:
        sleep = acquire_lock(bucketuri)
        if sleep > 0:
            # After sleeping we need to test if we are still the latest commit.
            latest_sha = GitHub.get_latest_sha(owner=github.owner, repo=github.repo,
                                               token=github.token, ref=github.ref)
            if not github.sha == latest_sha:
                logger.warning('After waiting to aquire the lock for S3 we are not ' +
                               'the latest commit any more: event-sha: ' + github.sha +
                               ' latest-sha: ' + latest_sha)
                github.set_status('failure', 'Stopping build because there is a newer commit')
                github.create_commit_comment(':warning: **Stopping build because there is a newer commit**')
                return

        logger.info('Syncing to S3 bucket: ' + bucketuri)
        sync_time = time.time()
        s_out = subprocess.check_output('python /var/task/s3cmd/s3cmd sync ' +
                                        '--delete-removed --no-mime-magic ' +
                                        '--no-preserve ' + pushdir + ' ' +
                                        bucketuri, shell=True, stderr=subprocess.STDOUT)
        logger.info('Sync output:\n' + s_out)
        sync_time = time.time() - sync_time
    except subprocess.CalledProcessError as e:
        github.set_status('error', 'Failed to sync generated site to Amazon S3')
        github.create_commit_comment(':x: **Failed to sync generated site to ' +
                                     'Amazon S3**\n\n' + e.output)
        raise Exception('Failed to sync generated site to Amazon S3: ' +
                        e.output)
    finally:
        release_lock(bucketuri)


    # 5. Success!
    github.set_status('success', 'Successfully generated and deployed static site')
    total_time = time.time() - total_time
    stats = 'Triggered by: ' + github.event_type + '\n' + \
            'Start time (UTC): ' + start_datetime.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + '\n' + \
            'Repo download size: ' + str(download_size / 1000) + ' kilobytes\n' + \
            'Repo download duration: ' + '%.3f' % download_time + ' seconds\n' + \
            'Hugo build duration: ' + '%.3f' % hugo_time + ' seconds\n' + \
            'S3 sync duration: ' + '%.3f' % sync_time + ' seconds\n' + \
            'Total duration: ' + '%.3f' % total_time + ' seconds'
    logger.info('Successfully generated and deployed static site\n' + stats)
    icon = ':repeat:' if github.event_type == "Scheduled Event" else ':white_check_mark:'
    github.create_commit_comment(icon + ' **Sucessfully generated ' +
             'and deployed static site**\n\n' + stats)


def acquire_lock(bucket):
    """ Tries to get a write lock for the specified bucket on DynamoDB.

    If the lock was already acquired from another Lambda function, this
    function sleeps and tries again. If the lock could be acquired, the
    function returns.

    Returns the total sleep time necessary until the lock could be aquired.
    """
    sleep = 0
    try:
        while True:
            lock_item = dynamodb_client.get_item(TableName='lambdaLocks',
                                                 Key={'id':{'S':bucket}},
                                                 ConsistentRead=True)

            if 'Item' not in lock_item:
                # No lock item, we acquire it.
                create_lock_item(bucket)
                return sleep

            lock_date = datetime.strptime(lock_item['Item']['created']['S'], "%Y-%m-%d %H:%M:%S")

            # Check if the lock item is more than 300s old (the max execution
            # duration of a Lambda function).
            if lock_date + timedelta(seconds=300) < datetime.utcnow():
                # Just overwrite the invalid lock item with our own.
                logger.error('Lock item for bucket "' + bucket + '" was older ' +
                             'than 300s (' + lock_date + '). A Lambda function ' +
                             'did not release it. I will overwrite it.')
                create_lock_item(bucket)
                return sleep

            # Sleep for a few seconds.
            logger.info('Waiting to acquire lock for S3. Sleeping for 5 seconds')
            time.sleep(5)
            sleep += 5
    except Exception as e:
        logger.warning('Could not acquire lock for S3. Will write anyway ' +
                       'and hope nothing bad happens: ' + str(e))

    return sleep

def create_lock_item(bucket):
    logger.info('Acquiring lock item for writing to S3.')
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    dynamodb_client.put_item(TableName='lambdaLocks',
                             Item={'id':{'S':bucket}, 'created':{'S':now}})


def release_lock(bucket):
    logger.info('Releasing lock item for S3.')
    dynamodb_client.delete_item(TableName='lambdaLocks', Key={'id':{'S':bucket}})


class GitHub(object):
    """ Class for accessing GitHub."""

    def __init__(self, event, context):
        """ Initializes the GitHub object."""

        # Get the GitHub token from the Lambda function description.
        self._read_function_description(context)

        # Determine the event type.
        if 'event_type' in event and event['event_type'] == 'scheduled':
            # We've got a scheduled event.
            self.event_type = 'Scheduled Event'
            self._init_scheduled_event(event)
        else:
            # Must be a GitHub event.
            try:
                sns = event['Records'][0]['Sns']
                github_event = sns['MessageAttributes']['X-Github-Event']['Value']
                message = json.loads(sns['Message'])
            except Exception as e:
                raise EventInvalidException('Unknown Event causing the Lambda function call:\n' +
                                            str(event))

            self.event_type = 'GitHub Push Event'
            self._init_github_event(github_event, message)


    def _read_function_description(self, context):
        """Reads the description from the Lambda function console.

        This is a workaround to get some sort of runtime configuration.
        The description contains:
        * The GitHub access token "github_token" (required)
        * Draft "draft" (optional)
        """

        lambda_desc = lambda_client.get_function_configuration(
                FunctionName=context.function_name)['Description']

        try:
            lambda_desc_json = json.loads(lambda_desc)
        except ValueError as e:
            raise Exception('No valid GitHub access token found. Please provide ' +
                            'the access token as the Lambda function discription. ' +
                            'Example: {"github_token": "..."}')

        if 'github_token' in lambda_desc_json:
            logger.info('Found GitHub access token in Lambda description.')
            self.token = lambda_desc_json['github_token']
        else:
            raise Exception('No valid GitHub access token found. Please provide ' +
                            'the access token as the Lambda function discription. ' +
                            'Example: {"github_token": "..."}')

        if 'draft' in lambda_desc_json:
            logger.info('Found draft property in Lambda function description: ' +
                        str(lambda_desc_json['draft']))
            self.draft = lambda_desc_json['draft']
        else:
            self.draft = False


    def _init_scheduled_event(self, event):
        """Initializes from a scheduled event."""

        logger.info('Received a scheduled event.')

        self.owner = event['owner']
        self.repo = event['repo']
        self.ref = event['ref']
        # Get the latest commit sha.
        self.sha = GitHub.get_latest_sha(owner=self.owner, repo=self.repo,
                                         token=self.token, ref=self.ref)


    def _init_github_event(self, github_event, message):
        """Initializes from a GitHub event."""

        # Tests if the event is a valid push event on the default branch.

        # Ignore non 'push' events.
        if not github_event == 'push':
            raise EventIgnoreException('Ignoring GitHub event type: ' +
                                       github_event)

        # Ignore newly created or deleted branches.
        if message['created']:
            raise EventIgnoreException('Ignoring branch "created" event.')
        if message['deleted']:
            raise EventIgnoreException('Ignoring branch "deleted" event.')

        # Ignore non-default branches.
        ref = message['ref']
        branch = ref[ref.rfind('/') + 1:]
        default_branch = message['repository']['default_branch']
        if not branch == default_branch:
            raise EventIgnoreException('Ignoring events of non-default ' +
                                       'branch: ' + branch +
                                       ', default branch: ' + default_branch)

        # Init the properties.
        self.owner = message['repository']['owner']['name']
        self.repo = message['repository']['name']
        self.sha = message['head_commit']['id']
        self.ref = ref[ref.find('/') + 1:]

        # Ignore if it isn't the latest commit.
        # When an exception was thrown, Lambda automatically does two retries.
        # This can lead to a situation where the retry does not contain the
        # most recent commit.
        latest_sha = GitHub.get_latest_sha(owner=self.owner, repo=self.repo,
                                         token=self.token, ref=self.ref)
        if not self.sha == latest_sha:
            raise EventIgnoreException('Ignoring event because it does not contain the ' +
                                       'latest commit: event-sha: ' + self.sha +
                                       ' latest-sha: ' + latest_sha)

        # We have a valid GitHub event.
        logger.info('Received a valid GitHub push event on default branch: ' +
                    branch + ' (' + ref + ')')


    @property
    def commit_url(self):
        return 'https://github.com/' + self.repo_full_name + '/commit/' + self.sha + '/'


    @property
    def repo_full_name(self):
        return self.owner + '/' + self.repo


    @staticmethod
    def get_latest_sha(owner, repo, token, ref='heads/master'):
        """Returns the latest commit sha.

        This function makes a request to the GitHub Api.

        Attributes:
            owner   The owner of the GitHub repo.
            repo    The repository name.
            token   The GitHub access token.
            ref     A GitHub reference. The default is 'heads/master'.
        """

        url = 'https://api.github.com/repos/' + owner + '/' + repo + \
              '/git/refs/' + ref
        req = urllib2.Request(url)
        req.add_header('Authorization', 'token ' + token)
        response = json.load(urllib2.urlopen(req))
        return response['object']['sha']


    def download(self):
        """Downloads the latest commit from GitHub.

        Returns the directory string of the downloaded content.
        """

        url = 'https://api.github.com/repos/' + self.repo_full_name + \
              '/zipball/' + self.sha
        req = urllib2.Request(url)
        req.add_header('Authorization', 'token ' + self.token)

        logger.info('Download request: ' + url)

        # Download zip.
        with open('/tmp/repo.zip', 'wb') as f:
            f.write(urllib2.urlopen(req).read())

        download_size = os.path.getsize('/tmp/repo.zip')

        # Unzip.
        zfile = zipfile.ZipFile('/tmp/repo.zip')
        try:
            zfile.extractall('/tmp')
        except Exception as e:
            github.set_status('error', 'Failed to generate site with Hugo')
            github.create_commit_comment(':x: **Failed to extract Repo Zip ' +
                                         'from GitHub**\n\n' + e.output)
            raise Exception('Failed to extract Repo Zip from GitHub: ' +
                            e.output)

        os.remove('/tmp/repo.zip')

        # Get the folder name of the extracted repository.
        directory = '/tmp/' + self.owner + '-' + self.repo + '-' + self.sha
        #directory = '/tmp/unzipped/' + os.listdir('/tmp/unzipped')[0]

        return (directory, download_size)


    def set_status(self, state, description=''):
        """Sets the commit status.

        Possible states are: pending, success, error, or failure.
        """

        if self.draft:
            logger.info('Ignoring set_status for drafts')
            return

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
        req.add_header('Authorization', 'token ' + self.token)
        req.add_header('Content-Type', 'application/json')
        urllib2.urlopen(req)


    def create_deployment(self):
        """ Creates a deployment and returns the deployment id."""

        payload = {
          'ref': self.sha
        }

        url = 'https://api.github.com/repos/' + self.repo_full_name + \
              '/deployments'
        req = urllib2.Request(url, json.dumps(payload))
        req.add_header('Authorization', 'token ' + self.token)
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
        req.add_header('Authorization', 'token ' + self.token)
        req.add_header('Content-Type', 'application/json')
        urllib2.urlopen(req)


    def create_commit_comment(self, comment):
        """ Creates a commit message."""

        if self.draft:
            comment = ':construction: **Generated with DRAFTS and FUTURE CONTENT' + \
                      '** :construction:\n\n---\n\n ' + comment

        payload = {
          'body': comment
        }

        url = 'https://api.github.com/repos/' + self.repo_full_name + \
              '/commits/' + self.sha + '/comments'
        req = urllib2.Request(url, json.dumps(payload))
        req.add_header('Authorization', 'token ' + self.token)
        req.add_header('Content-Type', 'application/json')
        urllib2.urlopen(req)


class EventInvalidException(Exception):
    """Exception raised for invalid events triggering the Lambda function.

    Attributes:
        msg  -- explanation of the error
    """

    def __init__(self, msg):
        self.msg = msg


class EventIgnoreException(Exception):
    """Exception raised for events triggering the Lambda function that are
    ignored. Examples are push events to a non-default branch on GitHub.

    Attributes:
        msg  -- explanation of the error
    """

    def __init__(self, msg):
        self.msg = msg
