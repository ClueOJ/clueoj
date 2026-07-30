#!/bin/sh
set -eu

: "${LETSENCRYPT_EMAIL:?Set LETSENCRYPT_EMAIL to the certificate contact email}"
: "${LETSENCRYPT_DOMAIN:?Set LETSENCRYPT_DOMAIN to the public hostname}"

mkdir -p /home/dmoj/vnoj-docker/dmoj/certbot/www

docker run --rm \
  -v /etc/letsencrypt:/etc/letsencrypt \
  -v /home/dmoj/vnoj-docker/dmoj/certbot/www:/var/www/certbot \
  certbot/certbot certonly --webroot -w /var/www/certbot \
  --non-interactive --email "$LETSENCRYPT_EMAIL" --agree-tos --no-eff-email \
  -d "$LETSENCRYPT_DOMAIN"
