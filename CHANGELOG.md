# Changelog
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](http://keepachangelog.com/en/1.0.0/)
and this project adheres to [Semantic Versioning](http://semver.org/spec/v2.0.0.html).


## [Unreleased]
### Added
  - API Key based authentication [#43](https://github.com/swisstopo/routing-graph-packager/pull/43)
  - Monitoring with Prometheus, StatsD and Grafana [#108](https://github.com/swisstopo/routing-graph-packager/pull/108)  
  - `/api/v1/readyz/` end point [#108](https://github.com/swisstopo/routing-graph-packager/pull/108)
### Fixed
  - finish graph build when `USE_ELEVATION=false` [#108](https://github.com/swisstopo/routing-graph-packager/pull/108)
### Changed
 - split the graph build into separate container, controlled via cron [#108](https://github.com/swisstopo/routing-graph-packager/pull/108)
### Deprecated
 - `VALHALLA_URL` environment variable [#108](https://github.com/swisstopo/routing-graph-packager/pull/108)
