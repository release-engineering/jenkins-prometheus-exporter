This is an experimental prometheus exporter for "jenkins" that exposes more per-job information than
the existing [prometheus plugin for jenkins](https://plugins.jenkins.io/prometheus/).

This polls jenkins looking for builds created since startup and exposes metrics about those builds
for a prometheus server.
