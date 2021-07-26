#!/usr/bin/env python3
""" A simple prometheus exporter for jenkins.

Scrapes jenkins on an interval and exposes metrics about builds.

It would be better for jenkins to offer a prometheus /metrics endpoint of its own.
"""

from datetime import datetime, timezone

import logging
import os
import time

import requests

from prometheus_client.core import (
    REGISTRY,
    CounterMetricFamily,
    GaugeMetricFamily,
    HistogramMetricFamily,
)
from prometheus_client import start_http_server

from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

retry_strategy = Retry(
    total=3,
    backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504],
    method_whitelist=["HEAD", "GET", "OPTIONS"]
)
adapter = HTTPAdapter(max_retries=retry_strategy)
session = requests.Session()
session.mount("https://", adapter)
session.mount("http://", adapter)

START = None

JENKINS_URL = os.environ['JENKINS_URL']  # Required
if 'JENKINS_USERNAME' in os.environ and 'JENKINS_TOKEN' in os.environ:
    JENKINS_USERNAME = os.environ['JENKINS_USERNAME']
    JENKINS_TOKEN = os.environ['JENKINS_TOKEN']
    AUTH = (JENKINS_USERNAME, JENKINS_TOKEN)
else:
    AUTH = None

DEFAULT_IGNORED = '00-all-enabled,01-all-disabled,all,All,My View'
IGNORED_VIEWS = os.environ.get('JENKINS_IGNORED_VIEWS', DEFAULT_IGNORED).split(',')

BUILD_LABELS = ['job', 'view']

BUILD_CACHE = {}

# In seconds
DURATION_BUCKETS = [
    10,
    30,
    60,  # 1 minute
    180,  # 3 minutes
    480,  # 8 minutes
    1200,  # 20 minutes
    3600,  # 1 hour
    7200,  # 2 hours
]

metrics = {}


error_states = [
    'FAILURE',
    'ABORTED',
    'UNSTABLE',
]
in_progress_states = [
    None,
]


class Incompletebuild(Exception):
    """ Error raised when a jenkins build is not complete. """
    pass


class Unmeasurablebuild(Exception):
    """ Error raised when a jenkins build is missing the measurement plugin. """
    pass


class GarbageCollectedBuild(Exception):
    """ Error raised when a jenkins build is just gone. """
    pass


def retrieve_recent_jenkins_builds(url):
    params = dict(
        tree="jobs[name,url,view,builds[number,timestamp,result,actions["
        "blockedTimeMillis,buildingDurationMillis]]],views[name,jobs[name]]",
    )
    url = url + '/api/json'
    response = session.get(url, params=params, auth=AUTH, timeout=30)
    response.raise_for_status()

    data = response.json()

    views = {}
    for view in data['views']:
        if view['name'].strip() in IGNORED_VIEWS:
            continue
        for job in view['jobs']:
            views[job['name']] = views.get(job['name'], [])
            views[job['name']].append(view['name'].strip())

    jobs = []
    builds = []
    for job in data['jobs']:
        if job['_class'] == 'com.cloudbees.hudson.plugins.folder.Folder':
            _jobs, _builds = retrieve_recent_jenkins_builds(job['url'])
            jobs.extend(_jobs)
            builds.extend(_builds)
            continue

        job['views'] = views.get(job['name'], ['no view defined'])
        jobs.append(job)

        for build in all_seen_jenkins_builds(job, job.get('builds', [])):
            cache_build_details(job, build)
            timestamp = datetime.fromtimestamp(build['timestamp'] / 1000.0, tz=timezone.utc)
            if timestamp < START:
                break
            build['job'] = job['name']
            build['views'] = job['views']
            builds.append(build)

    return jobs, builds


def cache_build_details(job, build):
    if job['name'] not in BUILD_CACHE:
        BUILD_CACHE[job['name']] = {}

    if build['number'] not in BUILD_CACHE[job['name']]:
        if build['result'] in in_progress_states:
            # Skip populating cache for incomplete builds.
            return build
        BUILD_CACHE[job['name']][build['number']] = build

    return BUILD_CACHE[job['name']][build['number']]


def all_seen_jenkins_builds(job, builds):
    seen = []
    seen = [build['number'] for build in builds]
    for build in builds:
        timestamp = datetime.fromtimestamp(build['timestamp'] / 1000.0, tz=timezone.utc)
        if timestamp < START:
            break
        yield build
    for number in reversed(sorted(BUILD_CACHE.get(job['name'], {}).keys())):
        if number in seen:
            continue
        build = BUILD_CACHE[job['name']][number]
        timestamp = datetime.fromtimestamp(build['timestamp'] / 1000.0, tz=timezone.utc)
        if timestamp < START:
            break
        yield build


def jenkins_builds_total(jobs, builds):

    # Initialize with zeroes.
    counts = {}
    for job in jobs:
        counts[job['name']] = counts.get(job['name'], {})
        for view in job['views']:
            counts[job['name']][view] = counts[job['name']].get(view, 0)

    for build in builds:
        job = build['job']
        views = build['views']

        for view in views:
            counts[job][view] += 1

    for job in counts:
        for view in counts[job]:
            yield counts[job][view], [job, view]


def calculate_duration(build):
    if not build['result']:
        # Duration is undefined.
        # We could consider using `time.time()` as the duration, but that would produce durations
        # that change for incomlete builds -- and changing durations like that is incompatible with
        # prometheus' histogram and counter model.  So - we just ignore builds until they are
        # complete and have a final duration.
        raise Incompletebuild("build is not yet complete.  Duration is undefined.")
    for action in build['actions']:
        if action.get('_class') == 'jenkins.metrics.impl.TimeInQueueAction':
            return (action['blockedTimeMillis'] + action['buildingDurationMillis']) / 1000.0

    raise Unmeasurablebuild("No TimeInQueueAction plugin found")


def find_applicable_buckets(duration):
    buckets = DURATION_BUCKETS + ["+Inf"]
    for bucket in buckets:
        if duration < float(bucket):
            yield bucket


def jenkins_build_duration_seconds(jobs, builds):
    duration_buckets = DURATION_BUCKETS + ["+Inf"]

    # Build counts of observations into histogram "buckets"
    counts = {}
    # Sum of all observed durations
    durations = {}

    # Initialize with zeroes.
    for job in jobs:
        durations[job['name']] = durations.get(job['name'], {})
        counts[job['name']] = counts.get(job['name'], {})
        for view in job['views']:
            durations[job['name']][view] = durations[job['name']].get(view, 0)
            counts[job['name']][view] = counts[job['name']].get(view, {})
            for bucket in duration_buckets:
                counts[job['name']][view][bucket] = counts[job['name']][view].get(bucket, 0)

    for build in builds:
        job = build['job']
        views = build['views']

        try:
            duration = calculate_duration(build)
        except Incompletebuild:
            continue
        except Unmeasurablebuild:
            continue

        for view in views:
            # Increment applicable bucket counts and duration sums
            durations[job][view] += duration
            for bucket in find_applicable_buckets(duration):
                counts[job][view][bucket] += 1

    for job in counts:
        for view in counts[job]:
            buckets = [
                (str(bucket), counts[job][view][bucket])
                for bucket in duration_buckets
            ]
            yield buckets, durations[job][view], [job, view]


def only(builds, states):
    for build in builds:
        state = build['result']
        if state in states:
            yield build


def scrape():
    global START
    today = datetime.utcnow()
    START = datetime.combine(today, datetime.min.time())
    START = START.replace(tzinfo=timezone.utc)

    if START < startup:
        START = startup

    jobs, builds = retrieve_recent_jenkins_builds(JENKINS_URL)

    jenkins_builds_total_family = CounterMetricFamily(
        'jenkins_builds_total', 'Count of all jenkins builds', labels=BUILD_LABELS
    )
    for value, labels in jenkins_builds_total(jobs, builds):
        jenkins_builds_total_family.add_metric(labels, value)

    jenkins_build_errors_total_family = CounterMetricFamily(
        'jenkins_build_errors_total', 'Count of all jenkins build errors', labels=BUILD_LABELS
    )
    error_builds = only(builds, states=error_states)
    for value, labels in jenkins_builds_total(jobs, error_builds):
        jenkins_build_errors_total_family.add_metric(labels, value)

    jenkins_in_progress_builds_family = GaugeMetricFamily(
        'jenkins_in_progress_builds',
        'Count of all in-progress jenkins builds',
        labels=BUILD_LABELS,
    )
    in_progress_builds = only(builds, states=in_progress_states)
    for value, labels in jenkins_builds_total(jobs, in_progress_builds):
        jenkins_in_progress_builds_family.add_metric(labels, value)

    jenkins_build_duration_seconds_family = HistogramMetricFamily(
        'jenkins_build_duration_seconds',
        'Histogram of jenkins build durations',
        labels=BUILD_LABELS,
    )
    for buckets, duration_sum, labels in jenkins_build_duration_seconds(jobs, builds):
        jenkins_build_duration_seconds_family.add_metric(labels, buckets, sum_value=duration_sum)

    # Replace this in one atomic operation to avoid race condition to the Expositor
    metrics.update(
        {
            'jenkins_builds_total': jenkins_builds_total_family,
            'jenkins_build_errors_total': jenkins_build_errors_total_family,
            'jenkins_in_progress_builds': jenkins_in_progress_builds_family,
            'jenkins_build_duration_seconds': jenkins_build_duration_seconds_family,
        }
    )


class Expositor(object):
    """ Responsible for exposing metrics to prometheus """

    def collect(self):
        logging.info("Serving prometheus data")
        for key in sorted(metrics):
            yield metrics[key]


if __name__ == '__main__':
    now = datetime.utcnow()
    startup = now.replace(tzinfo=timezone.utc)

    logging.basicConfig(level=logging.DEBUG)
    for collector in list(REGISTRY._collector_to_names):
        REGISTRY.unregister(collector)
    REGISTRY.register(Expositor())

    # Popluate data before exposing over http
    scrape()
    start_http_server(8000)

    while True:
        time.sleep(int(os.environ.get('JENKINS_POLL_INTERVAL', '3')))
        scrape()
