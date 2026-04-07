#!/bin/sh
set -eu

mkdir -p /home/dmoj/vnoj-docker/dmoj/certbot/www

docker run --rm \
  -v /etc/letsencrypt:/etc/letsencrypt \
  -v /home/dmoj/vnoj-docker/dmoj/certbot/www:/var/www/certbot \
  certbot/certbot certonly --webroot -w /var/www/certbot \
  --non-interactive --email phancddev@gmail.com --agree-tos --no-eff-email \
  -d oj.clue.edu.vn
