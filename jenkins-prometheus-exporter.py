#!/usr/bin/env python3
""" A simple prometheus exporter for jenkins.

Scrapes jenkins on an interval and exposes metrics about builds.

It would be better for jenkins to offer a prometheus /metrics endpoint of its own.
"""

from datetime import datetime, timezone

import logging
import os
import time

import dogpile.cache
import requests

from prometheus_client.core import (
    REGISTRY,
    CounterMetricFamily,
    GaugeMetricFamily,
    HistogramMetricFamily,
)
from prometheus_client import start_http_server

cache = dogpile.cache.make_region().configure(
    'dogpile.cache.memory', expiration_time=1000
)

START = None

JENKINS_URL = os.environ['JENKINS_URL']  # Required
JENKINS_USERNAME = os.environ['JENKINS_USERNAME']  # Required
JENKINS_TOKEN = os.environ['JENKINS_TOKEN']  # Required
AUTH = (JENKINS_USERNAME, JENKINS_TOKEN)

BUILD_LABELS = ['job']

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


class GarbageCollectedBuild(Exception):
    """ Error raised when a jenkins build is just gone. """
    pass


@cache.cache_on_arguments()
def retrieve_jenkins_jobs(url):
    url = url + '/api/json'

    response = requests.get(url, auth=AUTH)
    response.raise_for_status()

    data = response.json()

    results = []
    for job in data['jobs']:
        if job['_class'] == 'com.cloudbees.hudson.plugins.folder.Folder':
            results.extend(retrieve_jenkins_jobs(job['url']))
        else:
            results.append(job)

    return results


def _retrieve_build_details(job, build):
    url = build['url'] + "/api/json"
    response = requests.get(url, auth=AUTH)
    if response.status_code == 404:
        raise GarbageCollectedBuild()
    response.raise_for_status()
    data = response.json()
    return data


def retrieve_build_details(job, build):
    if job['name'] not in BUILD_CACHE:
        BUILD_CACHE[job['name']] = {}

    if build['number'] not in BUILD_CACHE[job['name']]:
        details = _retrieve_build_details(job, build)
        if details['result'] in in_progress_states:
            # Skip populating cache for incomplete builds.
            return details
        BUILD_CACHE[job['name']][build['number']] = details

    return BUILD_CACHE[job['name']][build['number']]


def retrieve_all_seen_jenkins_builds(job):
    url = job['url'] + "/api/json"
    response = requests.get(url, auth=AUTH)
    response.raise_for_status()
    data = response.json()

    seen = []
    seen = [build['number'] for build in data['builds']]
    for build in data['builds']:
        yield build
    for number in reversed(sorted(BUILD_CACHE.get(job['name'], {}).keys())):
        if number in seen:
            continue
        yield BUILD_CACHE[job['name']][number]


def retrieve_recent_jenkins_builds(job):

    results = []
    for build in retrieve_all_seen_jenkins_builds(job):
        try:
            details = retrieve_build_details(job, build)
        except GarbageCollectedBuild:
            break

        timestamp = datetime.fromtimestamp(details['timestamp'] / 1000.0, tz=timezone.utc)
        if timestamp < START:
            break
        details['job'] = job['name']
        results.append(details)

    return results


def jenkins_builds_total(builds):
    counts = {}
    for build in builds:
        job = build['job']

        counts[job] = counts.get(job, 0)
        counts[job] += 1

    for job in counts:
        yield counts[job], [job]


def calculate_duration(build):
    if not build['result']:
        # Duration is undefined.
        # We could consider using `time.time()` as the duration, but that would produce durations
        # that change for incomlete builds -- and changing durations like that is incompatible with
        # prometheus' histogram and counter model.  So - we just ignore builds until they are
        # complete and have a final duration.
        raise Incompletebuild("build is not yet complete.  Duration is undefined.")
    for action in build['actions']:
        if action['_class'] == 'jenkins.metrics.impl.TimeInQueueAction':
            return (action['blockedTimeMillis'] + action['buildingDurationMillis']) / 1000.0

    raise ValueError("No TimeInQueueAction plugin found")


def find_applicable_buckets(duration):
    buckets = DURATION_BUCKETS + ["+Inf"]
    for bucket in buckets:
        if duration < float(bucket):
            yield bucket


def jenkins_build_duration_seconds(builds):
    duration_buckets = DURATION_BUCKETS + ["+Inf"]

    # Build counts of observations into histogram "buckets"
    counts = {}
    # Sum of all observed durations
    durations = {}

    for build in builds:
        job = build['job']

        try:
            duration = calculate_duration(build)
        except Incompletebuild:
            continue

        # Initialize structures
        durations[job] = durations.get(job, 0)
        counts[job] = counts.get(job, {})
        for bucket in duration_buckets:
            counts[job][bucket] = counts[job].get(bucket, 0)

        # Increment applicable bucket counts and duration sums
        durations[job] += duration
        for bucket in find_applicable_buckets(duration):
            counts[job][bucket] += 1

    for job in counts:
        buckets = [
            (str(bucket), counts[job][bucket])
            for bucket in duration_buckets
        ]
        yield buckets, durations[job], [job]


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

    builds = []
    jobs = retrieve_jenkins_jobs(JENKINS_URL)
    for job in jobs:
        builds.extend(retrieve_recent_jenkins_builds(job))

    jenkins_builds_total_family = CounterMetricFamily(
        'jenkins_builds_total', 'Count of all jenkins builds', labels=BUILD_LABELS
    )
    for value, labels in jenkins_builds_total(builds):
        jenkins_builds_total_family.add_metric(labels, value)

    jenkins_build_errors_total_family = CounterMetricFamily(
        'jenkins_build_errors_total', 'Count of all jenkins build errors', labels=BUILD_LABELS
    )
    error_builds = only(builds, states=error_states)
    for value, labels in jenkins_builds_total(error_builds):
        jenkins_build_errors_total_family.add_metric(labels, value)

    jenkins_in_progress_builds_family = GaugeMetricFamily(
        'jenkins_in_progress_builds',
        'Count of all in-progress jenkins builds',
        labels=BUILD_LABELS,
    )
    in_progress_builds = only(builds, states=in_progress_states)
    for value, labels in jenkins_builds_total(in_progress_builds):
        jenkins_in_progress_builds_family.add_metric(labels, value)

    #jenkins_waiting_builds_family = GaugeMetricFamily(
    #    'jenkins_waiting_builds',
    #    'Count of all waiting, unscheduled jenkins builds',
    #    labels=BUILD_LABELS,
    #)
    #waiting_builds = retrieve_waiting_jenkins_builds()
    #for value, labels in jenkins_builds_total(waiting_builds):
    #    jenkins_waiting_builds_family.add_metric(labels, value)

    #jenkins_build_duration_seconds_family = HistogramMetricFamily(
    #    'jenkins_build_duration_seconds',
    #    'Histogram of jenkins build durations',
    #    labels=BUILD_LABELS,
    #)
    #for buckets, duration_sum, labels in jenkins_build_duration_seconds(builds):
    #    jenkins_build_duration_seconds_family.add_metric(labels, buckets, sum_value=duration_sum)

    # Replace this in one atomic operation to avoid race condition to the Expositor
    metrics.update(
        {
            'jenkins_builds_total': jenkins_builds_total_family,
            'jenkins_build_errors_total': jenkins_build_errors_total_family,
            'jenkins_in_progress_builds': jenkins_in_progress_builds_family,
            #'jenkins_waiting_builds': jenkins_waiting_builds_family,
            #'jenkins_build_duration_seconds': jenkins_build_duration_seconds_family,
        }
    )


class Expositor(object):
    """ Responsible for exposing metrics to prometheus """

    def collect(self):
        logging.info("Serving prometheus data")
        for key in sorted(metrics):
            yield metrics[key]


if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG)
    for collector in list(REGISTRY._collector_to_names):
        REGISTRY.unregister(collector)
    REGISTRY.register(Expositor())

    # Popluate data before exposing over http
    scrape()
    start_http_server(8000)

    while True:
        time.sleep(int(os.environ.get('jenkins_POLL_INTERVAL', '3')))
        scrape()
