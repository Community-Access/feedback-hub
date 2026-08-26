#!/bin/sh
# Repair the database driver name FreeScout was installed with.
#
# **The bug.** The tiredofit/freescout image normalises DB_TYPE to the literal
# string "mariadb" (both `mariadb` and `mysql` inputs land there) and then
# writes `DB_CONNECTION=mariadb` into FreeScout's .env. FreeScout 1.8.219 runs
# on **Laravel 5.5**, whose config/database.php defines sqlite, mysql, pgsql
# and sqlsrv -- and no mariadb. A `mariadb` connection name only became a
# Laravel thing in version 11.
#
# So nothing can reach the database. What you see is not a database error: the
# app decides it is not installed and redirects every request to install.php,
# and APP_KEY is left empty because key generation needs a working config. Two
# symptoms, one cause, and neither of them says "database".
#
# There is no environment variable that avoids this, and the image's
# custom-scripts hook runs *before* the block that writes DB_CONNECTION, so it
# cannot be fixed from there either. Hence a script.
#
# **Idempotent and safe to run at any time.** It only ever rewrites a
# DB_CONNECTION whose value names a connection Laravel does not define, and it
# does nothing at all once FreeScout ships a version where mariadb is real.
#
#   ./fix-db-driver.sh              # against the running helpdesk-app
#   CONTAINER=other ./fix-db-driver.sh
set -eu

CONTAINER="${CONTAINER:-helpdesk-app}"
ENV_FILE=/www/html/.env
CONFIG=/www/html/config/database.php

say() { printf '%s\n' "$*"; }

current=$(docker exec "$CONTAINER" sh -c "grep -m1 '^DB_CONNECTION=' $ENV_FILE || true" | cut -d= -f2 | tr -d '\r')
if [ -z "$current" ]; then
    say "No DB_CONNECTION in $ENV_FILE -- nothing to repair (is the container up?)."
    exit 0
fi

# The authority is what Laravel actually defines, not a hardcoded list: the day
# upstream adds a real mariadb connection, this script stops touching anything.
if docker exec "$CONTAINER" sh -c "grep -qE \"^\\s+'${current}' => \\[\" $CONFIG"; then
    say "DB_CONNECTION=$current is a connection this FreeScout defines. Nothing to do."
    exit 0
fi

say "DB_CONNECTION=$current is not defined in config/database.php -- repairing to mysql."
docker exec "$CONTAINER" sh -c "sed -i 's/^DB_CONNECTION=.*/DB_CONNECTION=mysql/' $ENV_FILE"

# The key is empty whenever the driver was wrong on first boot, because key
# generation ran against a config that could not load. Generating one later is
# safe; regenerating an existing one would log every agent out and make every
# encrypted value unreadable, so it is strictly conditional.
if docker exec "$CONTAINER" sh -c "grep -q '^APP_KEY=base64:' $ENV_FILE"; then
    say "APP_KEY already set -- left alone."
else
    say "APP_KEY is empty -- generating."
    docker exec -u nginx "$CONTAINER" sh -c "cd /www/html && php artisan key:generate --force"
fi

docker exec -u nginx "$CONTAINER" sh -c "cd /www/html && php artisan config:clear && php artisan migrate --force"
say "Repaired. Run ./create-admin.sh if the administrator account was never created."
