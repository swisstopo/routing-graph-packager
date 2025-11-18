# Routing Graph Packager HTTP API

[![tests](https://github.com/swisstopo/routing-graph-packager/actions/workflows/test-ubuntu.yml/badge.svg)](https://github.com/swisstopo/routing-graph-packager/actions/workflows/test-ubuntu.yml)
[![Coverage Status](https://coveralls.io/repos/github/gis-ops/routing-graph-packager/badge.svg)](https://coveralls.io/github/gis-ops/routing-graph-packager)

A [FastAPI](https://github.com/tiangolo/fastpi) app to dynamically deliver ZIPped [Valhalla](https://github.com/valhalla/valhalla) graphs from a variety of data sources.

The default road dataset is the [OSM](openstreetmap.org) planet PBF. If available, it also supports road datasets of commercial vendors, such as TomTom and HERE, assuming they are provided in the [OSM PBF format](https://wiki.openstreetmap.org/wiki/PBF_Format#).

## Features

- **user store**: with basic authentication for `POST` and `DELETE` endpoints
- **bbox extracts**: generate routing packages within a bounding box
- **data updater**: includes a daily OSM updater
- **asynchronous API**: graph generation is outsourced to a [`ARQ`](https://github.com/samuelcolvin/arq) worker
- **email notifications**: notifies the requesting user if the job succeeded/failed
- **logs API** read the logs for the worker, the app and the graph builder via the API
- **api key based authentication**: for reading/creating jobs

## "Quick Start"

The following will download

First you need to clone the project:

```
git clone https://github.com/gis-ops/routing-graph-packager.git
```

Since the graph generation takes place in docker containers, you'll also need to pull the relevant image: `docker pull ghcr.io/gis-ops/docker-valhalla/valhalla:latest`.

The easiest way to quickly start the project is to use `docker-compose`:

```
docker-compose up -d
```

With the project defaults, you can now make a `POST` request which will generate a graph package in `DATA_DIR` (default `./data`) from Andorra:

```
curl --location -XPOST 'http://localhost:5000/api/v1/jobs' \
--header 'Authorization: Basic YWRtaW5AZXhhbXBsZS5vcmc6YWRtaW4=' \
--header 'Content-Type: application/json' \
--data-raw '{
	"name": "test",  # name needs to be unique for a specific router & provider
	"description": "test descr",
	"bbox": "1.531906,42.559908,1.6325,42.577608",  # the bbox as minx,miny,maxx,maxy
	"provider": "osm",  # the dataset provider, needs to be registered in ENABLED_PROVIDERS
	"update": "true"  # whether this package should be updated on every planet build
}'
```

After a minute you should have the graph package available in `./data/output/osm_test/`. If not, check the logs of the worker process or the Flask app.

The `routing-packager-app` container running the HTTP API has a `supervisor` process running in a loop, which:

- downloads a planet PBF (if it doesn't exist) or updates the planet PBF (if it does exist)
- builds a planet Valhalla graph
- then updates all graph extracts with a fresh copy

By default, also a fake SMTP server is started, and you can see incoming messages on `http://localhost:1080`.

## Concepts

### Graph & OSM updates

Under the hood we're running a `supervisor` instance to control the graph builds.

Two instances of the [Valhalla docker image](https://github.com/gis-ops/docker-valhalla) take turns building a new graph from an updated OSM file. Those two graphs are physically separated from each other in subdirectories `$TMP_DATA_DIR/osm/8002` & `$TMP_DATA_DIR/osm/8003`.

After each graph build finished, the OSM file is updated for the next graph build.

### Data sources

This service tries to be flexible in terms of data sources and routing engines. Consequently, we support proprietary dataset such as from TomTom or HERE.

However, all data sources **must be** in the OSM PBF format and follow the OSM tagging model. There is [commercial support](https://github.com/gis-ops/prop2osm) in case of interest.

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

The app exposes logs via the route `/api/v1/logs/{log_type}`. Available log types are `worker`, `app` and `builder`. An optional query parameter `?lines={n}` limits the output to the last `n` lines. Authentication is required.

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

