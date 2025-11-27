#!/usr/bin/env bash

# Author: Nils Nolde <nils@gis-ops.com>
# Updated: 15-12-2022
# License: MIT
#
# Rotationally build Valhalla tiles to 2 different directories (env vars from docker-compose.yml).
# Respects a .lock file and waits for it to disappear before nuking the tileset.
#
# Note, that some variables here are sourced from the .env/.docker_env file.
#
# Usage: ./run_valhalla.sh
#

# so it can see prime_server within the supervisor process
export LD_LIBRARY_PATH=/usr/local/lib

log_message() {
    echo "build_loop: $(date "+%Y-%m-%d %H:%M:%S") $1"
}

#set from .docker_env file
log_message "INFO: http_proxy set to $http_proxy"
log_message "INFO: https_proxy set to $https_proxy"

#set from .docker_env file in conf/valhalla.conf
log_message "INFO: CONCURRENCY set to $CONCURRENCY"
log_message "INFO: https_proxy set to $MAX_CACHE_SIZE"

# watch the .lock file every 10 secs
wait_for_lock() {
  count=0
  sleep=10
  max=3600
  while ! [[ $count == $max ]]; do
    if ! [[ -f $1/.lock ]]; then
     return
    fi
    echo "INFO: sleeping for $sleep seconds to wait for .lock file to disappear at $1"
    sleep $sleep
    count=$(( $count + $sleep ))
  done

  log_message "ERROR: max count reached"
  exit 1
}

# reset config so we don't start the service with a valid graph
# we only need the service to query the /status endpoint to decide
# which instance to shut down/start up
reset_config() {
  jq --arg d "" '.mjolnir.tile_dir = $d' "${valhalla_config}"| sponge "${valhalla_config}"
  jq --arg d "" '.additional_data.elevation = $d' "${valhalla_config}"| sponge "${valhalla_config}"
}

PORT_8002="8002"
PORT_8003="8003"
# $TMP_DATA_DIR needs to be defined, either by supervisor or the current shell
ELEVATION_DIR="$TMP_DATA_DIR/elevation"
VALHALLA_DIR_8002="$TMP_DATA_DIR/osm/$PORT_8002"
VALHALLA_DIR_8003="$TMP_DATA_DIR/osm/$PORT_8003"
# TODO: change PBF
PBF="/app/tmp_data/osm/planet-latest.osm.pbf"

# activate the virtual env so the CLI can do its job in the supervisor env
. /app/app_venv/bin/activate

CURRENT_PORT=""
CURRENT_VALHALLA_DIR=""
OLD_PORT=""
OLD_PID=""
iteration=0
while true; do
  (( iteration++ ))

  log_message "INFO: Starting iteration $iteration..."

  # Take 8002 if this is the first start
  # this is copied from FOSSGIS, was too lazy to change to smth more suitable
  if curl -fs --noproxy localhost "http://localhost:${PORT_8002}/status"; then
    CURRENT_PORT=${PORT_8003}
    OLD_PORT=${PORT_8002}
    CURRENT_VALHALLA_DIR=$VALHALLA_DIR_8003
  elif curl -fs --noproxy localhost "http://localhost:${PORT_8003}/status"; then
    CURRENT_PORT=${PORT_8002}
    OLD_PORT=${PORT_8003}
    CURRENT_VALHALLA_DIR=$VALHALLA_DIR_8002
  elif [[ $iteration != 1 ]]; then
    log_message "ERROR: Neither localhost:8002 nor localhost:8003 is up."
    exit 1
  else
    CURRENT_PORT=${PORT_8002}
    CURRENT_VALHALLA_DIR=$VALHALLA_DIR_8002
  fi

  if ! [[ -d $CURRENT_VALHALLA_DIR ]]; then
    mkdir -p $CURRENT_VALHALLA_DIR
  fi

  # download the PBF file if need be
  UPDATE_OSM="True"
  if ! [ -f "$PBF" ]; then
    log_message "INFO: Downloading OSM file $PBF"
    wget -nv https://ftp5.gwdg.de/pub/misc/openstreetmap/planet.openstreetmap.org/pbf/planet-latest.osm.pbf -O "$PBF" || exit 1
    # wget -nv https://ftp5.gwdg.de/pub/misc/openstreetmap/download.geofabrik.de/germany-latest.osm.pbf -O "$PBF" || exit 1
    # wget -nv https://download.geofabrik.de/europe/iceland-latest.osm.pbf -O "$PBF" || exit 1
    # wget https://download.geofabrik.de/europe/andorra-latest.osm.pbf -O "$PBF" || exit 1
    UPDATE_OSM="False"
  fi

  if [[ $UPDATE_OSM == "True" ]]; then
    log_message "INFO: Updating OSM file $PBF"
    update_osm.sh -p "$PBF" || exit 1
  fi

  # build the current config
  log_message "INFO: Building valhalla.json to $CURRENT_VALHALLA_DIR"
  valhalla_config="$CURRENT_VALHALLA_DIR/valhalla.json"
  valhalla_build_config \
    --httpd-service-listen "tcp://*:${CURRENT_PORT}" \
    --loki-actions "status" \
    --mjolnir-tile-extract "" \
    --mjolnir-tile-dir "$CURRENT_VALHALLA_DIR" \
    --additional-data-elevation "$ELEVATION_DIR" \
    --mjolnir-concurrency "$CONCURRENCY" \
    --mjolnir-max-cache-size "$MAX_CACHE_SIZE" \
    --mjolnir-logging-type "" \
    > "${valhalla_config}" || exit 1

  # wait until there's no .lock file anymore
  wait_for_lock "$CURRENT_VALHALLA_DIR"

  # If it's the first start and a graph already exists, continue with next build
  if [[ -z $OLD_PORT && -d $CURRENT_VALHALLA_DIR ]]; then
    # remove the reference to tiles_dir so the service doesn't actually load the tiles
    reset_config
    exec valhalla_service "$valhalla_config" 1 &
    OLD_PID=$!
    log_message "INFO: Started Valhalla the first time with config $valhalla_config on with PID $OLD_PID"
    sleep 10
    echo ""
    echo ""
    continue
  fi

  log_message "INFO: Building initial graph with $PBF..."
  valhalla_build_tiles -c "${valhalla_config}" -s initialize -e build "$PBF" || exit 1

  log_message "INFO: Downloading elevation to $ELEVATION_DIR..."
  valhalla_build_elevation --from-tiles --decompress -c ${valhalla_config} -v || exit 1
  # debugging with andorra only:
  # valhalla_build_elevation --decompress -c ${valhalla_config} -v -b 1,42,2,43 || exit 1

  log_message "INFO: Enhancing initial tiles with elevation..."
  valhalla_build_tiles -c "${valhalla_config}" -s enhance -e cleanup "$PBF" || exit 1

  # reset config so the service won't load the graph
  reset_config

  log_message "INFO: Updating the registered packages with $(which python3)"
  python3 /app/cli.py

  # shut down the old service and launch the new one
  log_message "INFO: Killing Valhalla on port $OLD_PORT with PID $OLD_PID"
  kill -9 $OLD_PID
  exec valhalla_service "$valhalla_config" 1 &
  OLD_PID=$!
  log_message "INFO: Started Valhalla on port $CURRENT_PORT with PID $OLD_PID"
  sleep 10

  echo ""
  echo ""

done
