#!/bin/bash

HUGO_VERSION=0.15
HUGO_FILE=hugo_${HUGO_VERSION}_linux_amd64

BASEDIR=$(dirname $0)
DIR=$(mktemp -d)
cd $DIR

# Fetch s3cmd from our forked repo
wget https://github.com/makery/s3cmd/archive/master.zip
unzip master.zip
rm -f master.zip
mv s3cmd-master s3cmd


# Fetch hugo release (statically compiled go binary)
wget https://github.com/spf13/hugo/releases/download/v${HUGO_VERSION}/${HUGO_FILE}.tar.gz
tar -xf ${HUGO_FILE}.tar.gz
rm -f ${HUGO_FILE}.tar.gz
mv ${HUGO_FILE}/${HUGO_FILE} ${HUGO_FILE}.go
rm -rf ${HUGO_FILE}
# cleanup
find . -name "*.pyc" -delete

# Use the local main script
# wget https://raw.githubusercontent.com/jolexa/hugo-lambda-function/master/main.py
cp ${BASEDIR}/main.py ./

# create zip
zip -r9 ${BASEDIR}/hugo-lambda-function.zip *
cd ..
# cleanup
rm -rf $DIR
