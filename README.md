# Routing Graph Packager HTTP API

[![tests](https://github.com/swisstopo/routing-graph-packager/actions/workflows/test-ubuntu.yml/badge.svg)](https://github.com/swisstopo/routing-graph-packager/actions/workflows/test-ubuntu.yml)
[![Coverage Status](https://coveralls.io/repos/github/gis-ops/routing-graph-packager/badge.svg)](https://coveralls.io/github/gis-ops/routing-graph-packager)

A [FastAPI](https://github.com/tiangolo/fastpi) app to dynamically deliver ZIPped [Valhalla](https://github.com/valhalla/valhalla) graphs from a variety of data sources.

The default road dataset is the [OSM](openstreetmap.org) planet PBF. If available, it also supports road datasets of commercial vendors, such as TomTom and HERE, assuming they are provided in the [OSM PBF format](https://wiki.openstreetmap.org/wiki/PBF_Format#).

## Features

- **user store**: with basic authentication for `POST` and `DELETE` endpoints
- **bbox extracts**: generate routing packages within a bounding box
- **data updater**: includes a scheduled OSM updater
- **asynchronous API**: graph generation is outsourced to a [`ARQ`](https://github.com/samuelcolvin/arq) worker
- **email notifications**: notifies the requesting user if the job succeeded/failed
- **logs API** read the logs for the worker, the app and the graph builder via the API
- **api key based authentication**: for reading/creating jobs
- **metrics**: StatsD out of the app and out of Valhalla itself, with a Prometheus/Grafana stack in the compose file

## Quick Start

First you need to clone the project:

```
git clone git@github.com:swisstopo/routing-graph-packager.git
```

The easiest way to quickly start the project locally is to use Docker Compose:

```sh
docker compose -f docker-compose.local.yml up -d --build
```

First, the graph builder needs some time to build the first Valhalla graph. You can check in on the status of the builder using the `api/v1/readyz` end point, a machine readable end point that tells whether the worker is ready to perform packaging work. A 200 HTTP response code means the system is operational, a 503 means it isn't. For more information on readyz and health end points, see further down.

With the project defaults, you can now make a `POST` request which will generate a graph package in `DATA_DIR` (default `./data`) from Albania (or another country if you've edited the compose file):

```bash
curl --location -XPOST 'http://localhost:5000/api/v1/jobs' \
--header 'Authorization: Basic YWRtaW5AZXhhbXBsZS5vcmc6YWRtaW4=' \
--header 'Content-Type: application/json' \
# "name": needs to be unique for a specific router & provider
# "bbox": format is minx,miny,maxx,maxy
# "provider": currently only osm is supported
# "update": whether this package should be updated on every graph build
--data-raw '{
	"name": "test",
	"description": "my test package,
	"bbox": "1.531906,42.559908,1.6325,42.577608", 
	"provider": "osm",
	"update": "true"
}'
```

Shortly after, you should have the graph package available in `./local_data/data/output/osm_test` (format is `{privider}_{name}`.

A separate `routing-packager-graph-build` container runs on the schedule set by `GRAPH_BUILD_CRON`, and on each run:

- downloads a PBF (if it doesn't exist yet) or updates the PBF (if it does exist)
- builds a Valhalla graph into a fresh generation directory
- atomically points the `graph` symlink at it
- then queues an update of all updatable graph extracts with the fresh graph

By default, also a fake SMTP server is started, and you can see incoming messages on `http://localhost:1080`.

## Concepts

### Graph & OSM updates

The graph build runs in its own container on a schedule you control with `GRAPH_BUILD_CRON` (a standard 5-field cron expression, default `0 3 * * 0`: Sundays at 03:00). Because the build loop sleeps in-process until the next occurrence, two builds can never overlap, however long a planet build takes.

Before each build the PBF is brought up to date with [`pyosmium-up-to-date`](https://docs.osmcode.org/pyosmium/latest/tools_uptodate.html), which reads the replication server and sequence number straight from the PBF's own osmosis headers.

#### Graph generations

A **generation** refers to a finished graph on disk (plus some metadata), represented as a directory with a timestamp name.

Every build writes into a **new** directory, `$TMP_DATA_DIR/osm/generations/<timestamp>/`. Nothing is ever overwritten in place, so no packaging job can be reading a directory while it is being written to. When the build succeeds, the symlink `$TMP_DATA_DIR/osm/graph` is re-directed at it with a single atomic `rename(2)`. That symlink is the source of truth for "which graph is current".

A failed build simply leaves the symlink alone, so the previous graph keeps serving packages.

Old generations are deleted at the **start** of the next build. On top of that, deletion is guarded with a lock, one per generation: the builder must hold it exclusively before removing a generation, and every worker holds the same lock shared for as long as it is packaging tiles from it. A second lock, one per provider, admits a single graph build at a time.

The locks live in Postgres, so the builder does not have to share a machine with the workers.

In order to avoid deadlocks on failed graph builds, there is a lock expiry `GRAPH_LOCK_TTL` seconds after a lock was taken, and expired rows are cleaned up by the next lock taken on anything. Note that `GRAPH_LOCK_TTL` has to be shorter than the interval between builds. In the meantime the state is plain SQL:

```sql
SELECT path, mode, holder, expires_at FROM graph_locks;
```

and a lock that is stuck is a `DELETE` away.

Pruning of graphs is done *before* the build starts, so there are at most 2 full graphs on disk at any time. If a generation is still being packaged, the builder waits up to `GRAPH_PRUNE_TIMEOUT` seconds for that job to finish. If it is still held after that, the build is aborted. An aborted build leaves the current graph serving and the next scheduled run tries again.

#### Running the build from an external scheduler

The builder normally loops in-process on `GRAPH_BUILD_CRON`, which is what the docker compose deployment wants. Where something else owns the schedule, e.g. a Kubernetes `CronJob`, simply pass `--once` instead and the container builds exactly one graph and exits:

```yaml
args: ["graph-build", "--once"]
```

`GRAPH_BUILD_CRON` is not read in this mode, and does not need to be valid. The builder has no way of knowing what the external scheduler was configured with, so it does not guess: `build.next_build_at` in the `/api/v1/health` report reads `externally_controlled` instead of a timestamp.

| Exit code | Meaning |
|---|---|
| `0` | The graph was built and the symlink swapped |
| `1` | The build failed; the previous graph keeps serving |
| `75` | Another build held the build lock, so this run did nothing |

Because 75 is non-zero, a scheduler that retries on failure will retry a run that was merely superfluous. Under Kubernetes, use the `--once` flag with `concurrencyPolicy: Forbid` and a low `backoffLimit`.

#### Relevant environment variables

| Variable | Default | Description |
|---|---|---|
| `GRAPH_BUILD_CRON` | `0 3 * * 0` | When to build, standard 5-field cron |
| `GRAPH_KEEP_GENERATIONS` | `1` | How many graph generations to keep when pruning |
| `GRAPH_PRUNE_TIMEOUT` | `3600` | Seconds to wait for a packaging job before aborting the build |
| `GRAPH_LOCK_TTL` | `345600` | Seconds a lock on a graph directory stays valid. Nothing renews it, so keep it shorter than the interval between builds |
| `PBF_URL` | planet.openstreetmap.org | Where to download the PBF from if it is missing |
| `PBF_LOCAL_PATH` | `$TMP_DATA_DIR/planet-latest.osm.pbf` | Where the PBF lives |
| `PBF_FORCE_UPDATE` | `false` | Pass `--force-update-of-old-planet`, needed for a very stale PBF |
| `PBF_UPDATE_SIZE_MB` | `1024` | Max diff size applied per `pyosmium-up-to-date` pass |
| `PBF_MAX_UPDATE_PASSES` | `10` | How many passes before giving up on catching up |
| `USE_ELEVATION` | `false` | Build elevation into the tiles |
| `CONCURRENCY` | `8` | Tile build threads |
| `MAX_CACHE_SIZE` | `1000000000` | Valhalla mjolnir cache size |

### Data sources

This service tries to be flexible in terms of data sources and routing engines. Consequently, we support proprietary dataset such as from TomTom or HERE.

However, all data sources **must be** in the OSM PBF format and follow the OSM tagging model. 

### `POST` new job

The app is listening on `/api/v1/jobs` for new `POST` requests to generate some graph according to the passed arguments. The lifecycle is as follows:

1. Request is parsed, inserted into the Postgres database and the new entry is immediately returned with a few job details as blank fields.
2. Before returning the response, the graph generation function is queued with `ARQ` in a Redis database to dispatch to a worker.
3. If the worker is currently
   - **idle**, the queue will immediately start the graph generation:
     - Pull the job entry from the Postgres database
     - Update the job's `status` database field along the processing to indicate the current stage
     - Zip graph tiles from disk according to the request's bounding box and put the package to `$DATA_DIR/output/<JOB_NAME>`, along with a metadata JSON
   - **busy**, the current job will be put in the queue and will be processed once it reaches the queue's head
4. Send an email to the requesting user with success or failure notice (including the error message)

### Logs

The app exposes logs via the route `/api/v1/logs/{log_type}`. Available log types are `worker`, `app` and `builder`. 

The `builder` log holds the full output of `valhalla_build_tiles` and the PBF update, not just the build loop's own messages. Every line coming from a subprocess is tagged: `[VALHALLA]` for all of Valhalla's binaries, `[WGET]` and `[PYOSMIUM-UP-TO-DATE]` for the others, so `grep '\[VALHALLA\]'` isolates a tile build, and `grep -v '\['` leaves the builders own messages. The graph builder additionally logs to stdout, so `docker logs routing-packager-graph-build` works too. An optional query parameter `?lines={n}` limits the output to the last `n` lines. Authentication is required.

All three log files rotate at 10 MB and keep 10 archives. The endpoint always serves the live file.

### Readiness

`GET /api/v1/readyz` answers whether this app instance can take packaging jobs. It needs **no authentication**, and the status code is all that matters:

| Code | Meaning |
|---|---|
| `200` | `{"ready": true}` |
| `503` | `{"ready": false}` |

It checks the four things the app itself needs to turn a submitted job into a queued one: 

1. a graph exists behind the symlink
2. Postgres answers
3. Redis answers,
4. the output directory is writable. 

A fresh deployment therefore reads `503` until the first graph build has finished.

### Health

`GET /api/v1/health` reports the graph being served, what the graph builder is doing, and whether the backing services are reachable. **Authentication is required**: basic auth or an `internal` API key. 

```json
{
  "status": "degraded",
  "graph": {
    "available": true,
    "path": "/app/tmp_data/osm/graph",
    "generation": "20260825T030000",
    "built_at": "2026-08-25T04:12:56.310000+00:00",
    "pbf_path": "/app/tmp_data/planet-latest.osm.pbf",
    "pbf_modified": "2026-08-25T03:01:44+00:00",
    "elevation": true,
    "valhalla_version": "3.8.3"
  },
  "build": {
    "state": "building",
    "stage": "building_tiles",
    "generation": "20260825T113047",
    "started_at": "2026-08-25T11:30:47+00:00",
    "updated_at": "2026-08-25T11:42:03+00:00",
    "next_build_at": null,
    "last_error": null
  },
  "services": {
    "postgres": {"up": true, "error": null},
    "redis": {"up": true, "error": null},
    "worker": {
      "up": false,
      "last_report": "Aug-21 11:41:20",
      "queued": 2,
      "ongoing": 0,
      "complete": 41,
      "failed": 1,
      "retried": 0
    }
  }
}
```

`status` is `ok` when a graph is available and Postgres, Redis and the worker are all up, `degraded` otherwise. The endpoint answers `200` either way. `/api/v1/readyz` signals through the status code.

#### `graph`

The generation that is currently symlinked, read from the `build_meta.json` the builder writes into it. `"available": false` means no graph has been built yet, or the symlink points at something unreadable. 

#### `build`

Read from `$TMP_DATA_DIR/<provider>/build_status.json`, which the graph build container rewrites atomically at every step.

| field | meaning |
|---|---|
| `state` | `idle`, `building`, `failed`, or `unknown` before the builder has ever run |
| `stage` | while building: `pruning`, `downloading_pbf`, `updating_pbf`, `building_tiles`, `building_elevation`, `enhancing_tiles` or `swapping` |
| `generation` | the directory the running build writes into, which is not yet the one being served |
| `updated_at` | when the builder was last heard from, refreshed at most every 10s while a build runs |
| `next_build_at` | while idle: the next `GRAPH_BUILD_CRON` occurrence, or `externally_controlled` when the builder runs with `--once` and something else owns the schedule |
| `last_error` | why the last build gave up, kept alongside the `stage` it died on |

#### `services`

Postgres is checked with a `SELECT 1`, Redis with a `PING`. The worker's entry comes from the health-check key ARQ uses. `"up": false` means the worker either stopped or has been unresponsive for more than a minute. `queued` is read live from the job queue.

### Monitoring

Metrics reach Prometheus two different ways, because the components are not alike.

**The app and the worker are scraped.** Both are long lived, so Prometheus pulls from them directly. Each process is its own scrape target, which means its metrics carry an `instance` label, the `up` metric says whether it is alive, and restarting one of them leaves the other's counters alone. The app serves `/metrics` on its own port; the worker has no HTTP surface of its own, so it starts a small server on `METRICS_PORT`.

**The graph builder pushes StatsD.** Prometheus pulls, and a `--once` build is a batch process that exits the instant after it records its result, which is exactly what a scrape would miss. The builder also runs Valhalla, which speaks [StatsD](https://github.com/statsd/statsd) and nothing else. Both send UDP to `statsd-exporter`, which Prometheus scrapes. Pull for services, push for batch.

| Variable | Default | Description |
|---|---|---|
| `METRICS_PORT` | `9101` | Port the worker serves `/metrics` on. `0` leaves it unscrapeable |
| `STATSD_HOST` | (empty) | Where the builder and Valhalla send metrics. Empty disables them |
| `STATSD_PORT` | `8125` | The collector's UDP port |
| `STATSD_PREFIX` | `rgp` | Prefixed to the builder's metrics. Valhalla's own always use `valhalla` |

`STATSD_HOST` no longer affects the app or the worker. Their `/metrics` endpoints are always served, which costs nothing when nobody scrapes them.

Both compose files ship a collector (`statsd-exporter`), a time series database (`prometheus`) and a dashboard (`grafana`). The local stack starts them along with everything else:

```bash
docker compose -f docker-compose.local.yml up -d
```

Grafana is then on [`localhost:3000`](http://localhost:3000) with the dashboard already provisioned, Prometheus on `localhost:9090`. The `statsd-exporter` in between is not published to the host: it listens on 8125/udp inside the private network only.

In `docker-compose.yml` the same three services sit behind the `monitoring` profile, so a deployment that does not want them is unaffected:

```bash
docker compose --profile monitoring up -d
```

Set `STATSD_HOST=statsd-exporter` in your `.env` when you enable the profile, and leave it unset when you do not. Pointing the builder at a collector that is not running costs nothing but a dropped packet — and a warning line per metric, which gets loud.

Two things to know before scaling anything:

- **`/metrics` is unauthenticated**, which is the convention, but `docker-compose.yml` publishes the app on 443. Nothing secret is in there, though endpoint names, request rates and latencies are. Gate it behind `BasicAuth` if that matters to you.
- **`gunicorn.py` sets `workers = 1`.** Raising it gives each worker process its own metric registry, and scrapes would land on whichever one answers. That needs `prometheus_client`'s multiprocess mode.

#### What is measured

Scraped from the app and the worker:

| Metric | Type | Labels |
|---|---|---|
| `rgp_http_requests_total` | counter | `method`, `endpoint`, `status` |
| `rgp_http_duration_seconds` | histogram | `method`, `endpoint` |
| `rgp_package_total` | counter | `outcome`, `update` |
| `rgp_package_duration_seconds` | histogram | `update` |

Pushed by the builder and by Valhalla, and mapped to Prometheus names in `monitoring/statsd_mapping.yml`:

| Metric | Becomes | Labels |
|---|---|---|
| `rgp.build.duration` | `rgp_build_duration_seconds` | `outcome` |
| `rgp.build.succeeded` / `.failed` / `.skipped` | `rgp_build_total` | `outcome` |
| `rgp.build.stage.duration` | `rgp_build_stage_duration_seconds` | `stage` |
| `valhalla.mjolnir.timing.*` | `valhalla_build_stage_duration_seconds` | `stage`, `provider` |

`update` distinguishes a package a user asked for (`false`) from one re-zipped by `update_all_packages` after a graph swap (`true`).

The last row is Valhalla's. `valhalla_build_tiles` times every one of its own stages and reports them itself; all the builder does is write a `statsd` block into each generation's `valhalla.json`, which it does whenever `STATSD_HOST` is set. Those are the same numbers the `[TIMING]` lines in the build log carry. The stages the builder owns — pruning, the PBF download and update, the symlink swap — are the ones Valhalla knows nothing about, and they arrive as `rgp.build.stage.duration` instead.

`monitoring/statsd_mapping.yml` only has to cover what is still pushed. It gives the build timers histogram buckets that suit their scale, hours rather than the seconds a default bucket set assumes, and folds the three outcome counters into one `rgp_build_total{outcome=...}`. Metrics matching no rule are still exported under a name derived from the StatsD one, so a missing rule loses the tuning, not the data. The scraped metrics need none of this: their buckets are declared in `routing_packager_app/metrics.py`, in seconds, where they can be read next to the code that fills them.

### Authentication and Authorization 

The REST API supports two methods of authentication: basic auth and api keys. 

#### Basic Auth 

Rather than a full fledged user management system, this method provides access to all routes for an admin user. 


#### API Keys 

For all non-admin users, access to either reading or reading and creating jobs can be granted by the admin user via issuing API keys. These keys can be created with a specific permission and validity duration in days. Furthermore, they can be annotated with comments. Finally, they can be revoked and their permissions and validity changed at any given time.

> **Note**: For security reasons, keys are not stored directly in the database. Instead, their hashes are stored. This means the raw key is only available once in the response of the key creation request. Afterwards, you will only be able to retrieve the hashed key, which is not usable for authentication. 

##### Examples 

###### Creating a new key 

```
curl --location -XPOST 'http://localhost:5000/api/v1/keys' \
--header 'Authorization: Basic <encoded_auth>' \
--header 'Content-Type: application/json' \
--data-raw '{
	"permission": "read",  # read, write or internal (for reading logs)
	"validity_days": 90,
	"comment": "issued to client XY"  # supports arbitrary comments
}'
```

The created key is returned as part of the response. Make sure to store the key, since this will be the only time it is accessible directly. In the DB, only its hash is stored. 


###### Retrieving a key 

Keys can either be found through their ID: 

```
curl --location -XPOST 'http://localhost:5000/api/v1/keys/<id>' \
--header 'Authorization: Basic <encoded_auth>' 
```

or using query parameters: 

```
curl --location -XPOST 'http://localhost:5000/api/v1/keys/?comment="client xy"' \
--header 'Authorization: Basic <encoded_auth>' 
```

```
curl --location -XPOST 'http://localhost:5000/api/v1/keys/?is_active=true'\
--header 'Authorization: Basic <encoded_auth>' 
```

###### Revoking a key 

Keys can be modified through PATCH requests: 

```
curl --location -XPATCH 'http://localhost:5000/api/v1/keys' \
--header 'Authorization: Basic <encoded_auth>' \
--header 'Content-Type: application/json' \
--data-raw '{
	"is_active": false 
}'
```

This method also allows changing a key's validity, comment or permission.

###### Passing a key 

When reading or creating jobs, you can pass an API key instead of a basic auth header like this: 

```
curl --location -XGET 'http://localhost:443/api/v1/jobs' --header 'x-api-key: H_I99kW7qqMATr5SGYTLAQ' --header 'Content-Type: application/json'
```

