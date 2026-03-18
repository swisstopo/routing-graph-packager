#!/usr/bin/env bash

# Author: Nils Nolde <nils@gis-ops.com>
# Updated: 15-12-2022
# License: MIT
#
# Update OSM PBF files in batch by specifying a folder and a fixed update interval
#
# Put in cron, e.g.
# (crontab -l || true; echo "0 3 * * * /path/to/this/script.sh > /some_log.txt") | crontab -
#

usage()
{
    echo "usage: update_osm.sh --pbf/-p /app/data/osm/planet-latest.osm.pbf"
}

log_message() {
    echo "build_loop: $(date "+%Y-%m-%d %H:%M:%S") $1"
}

log_message "update-osm HTTP_PROXY set to: $HTTP_PROXY"
log_message "update-osm HTTPS_PROXY set to: $HTTPS_PROXY"
log_message "update-osm PBF_LOCAL_PATH set to: $PBF_LOCAL_PATH"

pbf="$PBF_LOCAL_PATH"

# Get the arguments
while [ "$1" != "" ]; do
    case $1 in
        -p | --pbf )        shift
                                pbf=$1
                                ;;
        -h | --help )           usage
                                exit 0
                                ;;
        * )                     usage
                                exit 1
    esac
    shift
done

log_message "Updating ${pbf}"
fn=$(basename "${pbf}")
pbf_dir=$(dirname "$pbf")
pbf_name_updated="updated_${fn}"
pbf_updated="${pbf_dir}/${pbf_name_updated}"

# update the PBF
(osmupdate --day "${pbf}" "${pbf_updated}" && mv "${pbf_updated}" "${pbf}") || exit 0
