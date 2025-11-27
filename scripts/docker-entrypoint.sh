#!/usr/bin/env bash

cmd=${1}

# either starts a worker or the app itself
if [ "${cmd}" == 'worker' ]; then
  # Start the worker
  exec /app/app_venv/bin/arq routing_packager_app.worker.WorkerSettings
elif [ "${cmd}" == 'app' ]; then
  # SSL? Provided by .docker_env with path mapped in docker-compose.yml
  opts=''
  if [ -n "${SSL_CERT}" ] && [ -n "${SSL_KEY}" ]; then
    opts="--certfile ${SSL_CERT} --keyfile ${SSL_KEY}"
    echo "Provided SSL certificate ${SSL_CERT} with SSL key ${SSL_KEY}."
  else
    echo "No SSL configured."
  fi

  #Propagate env values for supervisord
  CONF_FILE="/etc/supervisor/conf.d/valhalla.conf"
  sed -i \
    -e "s|%(ENV_TMP_DATA_DIR)s|${TMP_DATA_DIR}|g" \
    -e "s|%(ENV_MAX_CACHE_SIZE)s|${MAX_CACHE_SIZE}|g" \
    -e "s|%(ENV_CONCURRENCY)s|${CONCURRENCY}|g" \
    "$CONF_FILE"
  
  # make sure the log directory exists for supervisor to be able to log
  mkdir -p /app/tmp_data/logs
  # Read the supervisor config and start the build loop
  service supervisor start
  supervisorctl start build_loop

  # Start the gunicorn server
  . /app/app_venv/bin/activate
  exec /app/app_venv/bin/gunicorn --config gunicorn.py ${opts} main:app
else
  echo "Command '${cmd}' not recognized. Choose from 'worker' or 'app'"
fi
