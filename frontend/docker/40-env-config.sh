#!/bin/sh
# Generates /env-config.js from the API_BASE_URL environment variable at container start,
# so one image can be pointed at any backend without rebuilding.
set -e
TARGET=/usr/share/nginx/html/env-config.js
if [ -n "${API_BASE_URL}" ]; then
  case "${API_BASE_URL}" in
    *\"* | *\\* | *\<* | *\>*)
      echo "env-config: API_BASE_URL contains invalid characters" >&2
      exit 1
      ;;
  esac
  printf 'window.__APP_CONFIG__ = { API_BASE_URL: "%s" };\n' "${API_BASE_URL}" > "${TARGET}"
  echo "env-config: API_BASE_URL=${API_BASE_URL}"
else
  echo "env-config: API_BASE_URL not set; using build-time VITE_API_BASE_URL"
fi
