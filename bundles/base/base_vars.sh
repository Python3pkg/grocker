#!/bin/bash

USER="blue"
GROUP="www-data"
USER_HOME=$( getent passwd "$USER" | cut -d: -f6 )

SYS_DEPS="
nginx-full
python
python3
sudo
supervisor
uwsgi-plugin-python
uwsgi-plugin-python3
virtualenv
"

BASE_DEPS="
libpq5
libgdal1h
libproj0

imagemagick
poppler-utils
libjpeg62-turbo
libffi6

libzbar0

libxml2
libxslt1.1

libldap-2.4-2
libsasl2-2
"

BUILD_DEPS="
build-essential
gettext

python-dev
python3-dev

nodejs-legacy

libpq-dev
libproj-dev

libjpeg62-turbo-dev
libffi-dev

libzbar-dev

libxml2-dev
libxslt1-dev

libldap2-dev
libsasl2-dev
"

export DEBIAN_FRONTEND=noninteractive
alias apt="apt -o APT::Install-Recommends=false -o APT::Install-Suggests=false"
