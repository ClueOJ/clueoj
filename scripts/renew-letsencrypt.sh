#!/bin/sh
set -eu

docker run --rm \
  -v /etc/letsencrypt:/etc/letsencrypt \
  -v /home/dmoj/vnoj-docker/dmoj/certbot/www:/var/www/certbot \
  certbot/certbot renew --non-interactive --webroot -w /var/www/certbot

docker exec vnoj_nginx nginx -s reload
