#!/usr/bin/env python3
""" A simple prometheus exporter for jenkins.

Scrapes jenkins on an interval and exposes metrics about builds.

It would be better for jenkins to offer a prometheus /metrics endpoint of its own.
"""

from datetime import datetime

import logging
import os
import time

import arrow
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

BUILD_LABELS = ['build_type']

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
    'error',
]
waiting_states = [
    'waiting',
]
in_progress_states = [
    'running',
]


class Incompletebuild(Exception):
    """ Error raised when a jenkins build is not complete. """
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


def retrieve_recent_jenkins_builds(job):
    url = job['url'] + "/api/json"

    raise NotImplementedError(
        "Working here.  How to avoid querying for every build individually to get "
        "details or figure out when to stop.  Is there no ?since= argument?")

    response = requests.get(url, auth=AUTH)
    response.raise_for_status()
    data = response.json()
    return data['jobs']


def retrieve_open_jenkins_builds():
    url = JENKINS_URL + '/jenkins/api/v2/builds/search/'
    query = {
        'criteria': {
            'fields': ['start_time', 'finish_time', 'state', 'build_type'],
            'filters': {
                'state': {
                    '$in': in_progress_states,
                },
            },
        },
    }
    response = requests.post(url, json=query, auth=AUTH)
    response.raise_for_status()
    return response.json()


def retrieve_waiting_jenkins_builds():
    url = JENKINS_URL + '/jenkins/api/v2/builds/search/'
    query = {
        'criteria': {
            'fields': ['start_time', 'finish_time', 'state', 'build_type'],
            'filters': {
                'state': {
                    '$in': waiting_states,
                },
            },
        },
    }
    response = requests.post(url, json=query, auth=AUTH)
    response.raise_for_status()
    return response.json()


def jenkins_builds_total(builds):
    counts = {}
    for build in builds:
        build_type = build['build_type']

        counts[build_type] = counts.get(build_type, 0)
        counts[build_type] += 1

    for build_type in counts:
        yield counts[build_type], [build_type]


def calculate_duration(build):
    if not build['finish_time']:
        # Duration is undefined.
        # We could consider using `time.time()` as the duration, but that would produce durations
        # that change for incomlete builds -- and changing durations like that is incompatible with
        # prometheus' histogram and counter model.  So - we just ignore builds until they are
        # complete and have a final duration.
        raise Incompletebuild("build is not yet complete.  Duration is undefined.")
    return (arrow.get(build['finish_time']) - arrow.get(build['start_time'])).total_seconds()


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
        build_type = build['build_type']

        try:
            duration = calculate_duration(build)
        except Incompletebuild:
            continue

        # Initialize structures
        durations[build_type] = durations.get(build_type, 0)
        counts[build_type] = counts.get(build_type, {})
        for bucket in duration_buckets:
            counts[build_type][bucket] = counts[build_type].get(bucket, 0)

        # Increment applicable bucket counts and duration sums
        durations[build_type] += duration
        for bucket in find_applicable_buckets(duration):
            counts[build_type][bucket] += 1

    for build_type in counts:
        buckets = [
            (str(bucket), counts[build_type][bucket])
            for bucket in duration_buckets
        ]
        yield buckets, durations[build_type], [build_type]


def only(builds, states):
    for build in builds:
        state = build['state']
        if state in states:
            yield build


def scrape():
    global START
    today = datetime.utcnow()
    START = datetime.combine(today, datetime.min.time()).isoformat()

    builds = retrieve_recent_jenkins_builds()

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
    in_progress_builds = retrieve_open_jenkins_builds()
    for value, labels in jenkins_builds_total(in_progress_builds):
        jenkins_in_progress_builds_family.add_metric(labels, value)

    jenkins_waiting_builds_family = GaugeMetricFamily(
        'jenkins_waiting_builds',
        'Count of all waiting, unscheduled jenkins builds',
        labels=BUILD_LABELS,
    )
    waiting_builds = retrieve_waiting_jenkins_builds()
    for value, labels in jenkins_builds_total(waiting_builds):
        jenkins_waiting_builds_family.add_metric(labels, value)

    jenkins_build_duration_seconds_family = HistogramMetricFamily(
        'jenkins_build_duration_seconds',
        'Histogram of jenkins build durations',
        labels=BUILD_LABELS,
    )
    for buckets, duration_sum, labels in jenkins_build_duration_seconds(builds):
        jenkins_build_duration_seconds_family.add_metric(labels, buckets, sum_value=duration_sum)

    # Replace this in one atomic operation to avoid race condition to the Expositor
    metrics.update(
        {
            'jenkins_builds_total': jenkins_builds_total_family,
            'jenkins_build_errors_total': jenkins_build_errors_total_family,
            'jenkins_in_progress_builds': jenkins_in_progress_builds_family,
            'jenkins_waiting_builds': jenkins_waiting_builds_family,
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
