#!/usr/bin/env bash

################################################################################################################
################################################################################################################
# COPY THE 1.0 Grading Website

echo -e "Copy the submission website"

if [ -z ${SUBMITTY_INSTALL_DIR+x} ]; then
    # constants are not initialized,
    CONF_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"/../../../config
    SUBMITTY_REPOSITORY=$(jq -r '.submitty_repository' ${CONF_DIR}/submitty.json)
    SUBMITTY_INSTALL_DIR=$(jq -r '.submitty_install_dir' ${CONF_DIR}/submitty.json)
    PHP_USER=$(jq -r '.php_user' ${CONF_DIR}/submitty_users.json)
    PHP_GROUP=${PHP_USER}
    CGI_USER=$(jq -r '.cgi_user' ${CONF_DIR}/submitty_users.json)
    CGI_GROUP=${CGI_USER}
fi

# copy the website from the repo. We don't need the tests directory in production and then
# we don't want vendor as if it exists, it was generated locally for testing purposes, so
# we don't want it
rsync -rtz --exclude 'tests' --exclude 'vendor' ${SUBMITTY_REPOSITORY}/site   ${SUBMITTY_INSTALL_DIR}

# clear old twig cache
if [ -d "${SUBMITTY_INSTALL_DIR}/site/cache/twig" ]; then
    rm -rf "${SUBMITTY_INSTALL_DIR}/site/cache/twig"
fi
# create twig cache directory
mkdir -p ${SUBMITTY_INSTALL_DIR}/site/cache/twig

# set special user $PHP_USER as owner & group of all website files
find ${SUBMITTY_INSTALL_DIR}/site -exec chown ${PHP_USER}:${PHP_GROUP} {} \;
find ${SUBMITTY_INSTALL_DIR}/site/cgi-bin -exec chown ${CGI_USER}:${CGI_GROUP} {} \;

# set the mask for composer so that it'll run properly and be able to delete/modify
# files under it
if [ -d "${SUBMITTY_INSTALL_DIR}/site/vendor/composer" ]; then
    chmod 640 ${SUBMITTY_INSTALL_DIR}/site/composer.lock
    chmod -R 740 ${SUBMITTY_INSTALL_DIR}/site/vendor
fi

# install composer dependencies and generate classmap
su - ${PHP_USER} -c "composer install -d \"${SUBMITTY_INSTALL_DIR}/site\" --no-dev --optimize-autoloader"

# TEMPORARY (until we have generalized code for generating charts in html)
# copy the zone chart images
mkdir -p ${SUBMITTY_INSTALL_DIR}/site/public/zone_images/
cp ${SUBMITTY_INSTALL_DIR}/zone_images/* ${SUBMITTY_INSTALL_DIR}/site/public/zone_images/ 2>/dev/null

# set the permissions of all files
# $PHP_USER can read & execute all directories and read all files
# "other" can cd into all subdirectories
chmod -R 440 ${SUBMITTY_INSTALL_DIR}/site
find ${SUBMITTY_INSTALL_DIR}/site -type d -exec chmod ogu+x {} \;

# "other" can read all of these files
array=( css otf jpg png ico txt twig )
for i in "${array[@]}"; do
    find ${SUBMITTY_INSTALL_DIR}/site/public -type f -name \*.${i} -exec chmod o+r {} \;
done

# "other" can read & execute these files
find ${SUBMITTY_INSTALL_DIR}/site/public -type f -name \*.js -exec chmod o+rx {} \;
find ${SUBMITTY_INSTALL_DIR}/site/cgi-bin -type f -name \*.cgi -exec chmod u+x {} \;

# cache needs to be writable
find ${SUBMITTY_INSTALL_DIR}/site/cache -type d -exec chmod u+w {} \;

# return the course index page (only necessary when 'clean' option is used)
if [ -f "$mytempcurrentcourses" ]; then
    echo "return this file! ${mytempcurrentcourses} ${originalcurrentcourses}"
    mv ${mytempcurrentcourses} ${originalcurrentcourses}
fi

#####################################
# Installing & building PDF annotator
pushd ${SUBMITTY_REPOSITORY}/../pdf-annotate.js
npm install
npm run-script build
if [ $DEBUGGING_ENABLED = true ]; then
    cp dist/pdf-annotate.js ${SUBMITTY_REPOSITORY}/site/public/js/pdf/pdf-annotate.js
    chmod -R 440 ${SUBMITTY_REPOSITORY}/site/public/js/pdf/pdf-annotate.js
    chown ${PHP_USER}:${PHP_GROUP} ${SUBMITTY_REPOSITORY}/site/public/js/pdf/pdf-annotate.js
else

    cp dist/pdf-annotate.min.js ${SUBMITTY_REPOSITORY}/site/public/js/pdf/pdf-annotate.min.js
    chmod -R 440 ${SUBMITTY_REPOSITORY}/site/public/js/pdf/pdf-annotate.min.js
    chown ${PHP_USER}:${PHP_GROUP} ${SUBMITTY_REPOSITORY}/site/public/js/pdf/pdf-annotate.min.js
fi


popd