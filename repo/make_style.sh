#!/bin/bash
if ! npx sass --version >/dev/null 2>&1; then
  echo 'Error: sass is not installed.' >&2
  exit 1
fi

if ! npx postcss --version >/dev/null 2>&1; then
  echo 'Error: postcss is not installed.' >&2
  exit 1
fi

if ! npx autoprefixer --version >/dev/null 2>&1; then
  echo 'Error: autoprefixer is not installed.' >&2
  exit 1
fi

cd "$(dirname "$0")" || exit

build_style() {
  echo "Creating $1 style..."
  cp resources/vars-$1.scss resources/vars.scss
  npx sass resources:sass_processed
  npx postcss sass_processed/style.css sass_processed/martor-description.css sass_processed/select2-dmoj.css --verbose --use autoprefixer -d $2
}

build_style 'default' 'resources'
build_style 'dark' 'resources/dark'
