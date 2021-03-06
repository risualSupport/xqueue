#!/usr/bin/env python
import json
import logging
import multiprocessing
import time
from datetime import datetime, timedelta
from queue.models import Submission
from queue.producer import get_queue_length

import pytz
import requests
from django.conf import settings
from django.db.models import Q
from django.utils import timezone
from requests.exceptions import ConnectionError, Timeout

log = logging.getLogger(__name__)


def get_single_unretired_submission(queue_name):
    '''
    Retrieve a single unretired queued item, if one exists, for the named queue

    Returns (success, submission):
        success:    Flag whether retrieval is successful (Boolean)
                    If no unretired item in the queue, return False
        submission: A single submission from the queue, guaranteed to be unretired
    '''

    # Look for submissions that haven't been pulled or were pulled more than SUBMISSION_PROCESSING_DELAY ago
    pull_time_filter = Q(pull_time__lte=(datetime.now(pytz.utc) - timedelta(minutes=settings.SUBMISSION_PROCESSING_DELAY))) | Q(pull_time__isnull=True)
    submission = Submission.objects.filter(pull_time_filter, queue_name=queue_name, retired=False).order_by('arrival_time').first()

    if submission:
        return (True, submission)
    else:
        return (False, '')


def post_failure_to_lms(header):
    '''
    Send notification to the LMS (and the student) that the submission has failed,
        and that the problem should be resubmitted
    '''

    # This is the only part of the XQueue that assumes knowledge of
    # the external grader message format.
    # TODO: Make the notification message-format agnostic
    msg = '<div class="capa_alert">'
    msg += 'Your submission could not be graded. '
    msg += 'Please recheck your submission and try again. '
    msg += 'If the problem persists, please notify the course staff.'
    msg += '</div>'
    failure_msg = {'correct': None,
                   'score': 0,
                   'msg': msg}
    return post_grade_to_lms(header, json.dumps(failure_msg))


def post_grade_to_lms(header, body):
    '''
    Send grading results back to LMS
        header:  JSON-serialized xqueue_header (string)
        body:    grader reply (string)

    Returns:
        success: Flag indicating successful exchange (Boolean)
    '''
    header_dict = json.loads(header)
    lms_callback_url = header_dict['lms_callback_url']

    payload = {'xqueue_header': header, 'xqueue_body': body}

    # Quick kludge retries to fix prod problem with 6.00x push graders. We're
    # seeing abrupt disconnects when servers are taken out of the ELB, causing
    # in flight lms_ack requests to fail. This just tries five times before
    # giving up.
    attempts = 0
    success = False
    while (not success) and attempts < 5:
        (success, lms_reply) = _http_post(lms_callback_url,
                                          payload,
                                          settings.REQUESTS_TIMEOUT)
        attempts += 1

    if not success:
        log.error("Unable to return to LMS: lms_callback_url: {0}, payload: {1}, lms_reply: {2}".format(lms_callback_url, payload, lms_reply))

    return success


def _http_post(url, data, timeout):
    '''
    Contact external grader server, but fail gently.

    Returns (success, msg), where:
        success: Flag indicating successful exchange (Boolean)
        msg: Accompanying message; Grader reply when successful (string)
    '''
    if settings.REQUESTS_BASIC_AUTH is not None:
        auth = requests.auth.HTTPBasicAuth(*settings.REQUESTS_BASIC_AUTH)
    else:
        auth = None

    try:
        r = requests.post(url, data=data, auth=auth, timeout=timeout, verify=False)
    except (ConnectionError, Timeout):
        log.error('Could not connect to server at %s in timeout=%f' % (url, timeout))
        return (False, 'cannot connect to server')

    if r.status_code not in [200]:
        log.error('Server %s returned status_code=%d' % (url, r.status_code))
        return (False, 'unexpected HTTP status code [%d]' % r.status_code)

    return (True, r.text)


class Worker(multiprocessing.Process):
    """Encapsulation of a single database montitor that listens on a queue
    """
    def __init__(self, queue_name, worker_url):
        super(Worker, self).__init__()

        self.queue_name = queue_name
        self.worker_url = worker_url

    def run(self):
        log.info("Starting consumer for queue {queue}".format(queue=self.queue_name))

        while True:
            # Look for submissions that haven't been pushed or were pushed more than 1 minute ago
            push_time_filter = Q(push_time__lte=(datetime.now(pytz.utc) - timedelta(minutes=settings.SUBMISSION_PROCESSING_DELAY))) | Q(push_time__isnull=True)
            submission = Submission.objects.filter(push_time_filter, queue_name=self.queue_name, retired=False).order_by('arrival_time').first()
            if submission:
                self._deliver_submission(submission)
            # Wait the given seconds between checking the database
            time.sleep(settings.CONSUMER_DELAY)

        log.info("Consumer for queue {queue} stopped".format(queue=self.queue_name))

    def _deliver_submission(self, submission):
        payload = {'xqueue_body': submission.xqueue_body,
                   'xqueue_files': submission.urls}

        submission.grader_id = self.worker_url
        submission.push_time = timezone.now()
        start = time.time()
        (grading_success, grader_reply) = _http_post(self.worker_url, json.dumps(payload), settings.GRADING_TIMEOUT)
        grading_time = time.time() - start

        if grading_time > settings.GRADING_TIMEOUT:
            log.error("Grading time above {} for submission. grading_time: {}s body: {} files: {}".format(settings.GRADING_TIMEOUT,
                      grading_time, submission.xqueue_body, submission.urls))

        job_count = get_queue_length(self.queue_name)

        submission.return_time = timezone.now()

        # TODO: For the time being, a submission in a push interface gets one chance at grading,
        #       with no requeuing logic
        if grading_success:
            submission.grader_reply = grader_reply
            submission.lms_ack = post_grade_to_lms(submission.xqueue_header, grader_reply)
        else:
            log.error("Submission {} to grader {} failure: Reply: {}, ".format(submission.id, self.worker_url, grader_reply))
            submission.num_failures += 1
            submission.lms_ack = post_failure_to_lms(submission.xqueue_header)

        # NOTE: retiring pushed submissions after one shot regardless of grading_success
        submission.retired = True

        submission.save()

    def __repr__(self):
        return "Worker (%r, %r)" % (self.worker_url, self.queue_name)
