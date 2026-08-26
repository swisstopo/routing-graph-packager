#!/usr/bin/env bash

cmd=${1}

if [ "${cmd}" == 'worker' ]; then
  exec /app/app_venv/bin/arq routing_packager_app.worker.WorkerSettings
elif [ "${cmd}" == 'graph-build' ]; then
  shift
  exec /app/app_venv/bin/python -m routing_packager_app.graph_build "$@"
elif [ "${cmd}" == 'app' ]; then
  opts=''
  if [ -n "${SSL_CERT}" ] && [ -n "${SSL_KEY}" ]; then
    opts="--certfile ${SSL_CERT} --keyfile ${SSL_KEY}"
    echo "Provided SSL certificate ${SSL_CERT} with SSL key ${SSL_KEY}."
  else
    echo "No SSL configured."
  fi

  mkdir -p /app/tmp_data/logs

  . /app/app_venv/bin/activate
  exec /app/app_venv/bin/gunicorn --config gunicorn.py ${opts} main:app
else
  echo "Command '${cmd}' not recognized. Choose from 'app', 'worker' or 'graph-build'"
fi
