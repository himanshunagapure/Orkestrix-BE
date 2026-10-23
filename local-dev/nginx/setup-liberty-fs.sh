#!/usr/bin/env bash
# Install and enable local nginx for LIBERTY_FS static file serving.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF_NAME="liberty-fs.conf"
LIBERTY_FS_ROOT="${LIBERTY_FS_ROOT:-/home/user/publicfolder/liberty_fs}"
LIBERTY_FS_PORT="${LIBERTY_FS_PORT:-843}"

echo "==> Liberty FS local nginx setup"
echo "    Root:  ${LIBERTY_FS_ROOT}"
echo "    Port:  ${LIBERTY_FS_PORT}"
echo "    URL:   http://localhost:${LIBERTY_FS_PORT}/{screen_id}"
echo

if ! command -v nginx >/dev/null 2>&1; then
    echo "==> Installing nginx..."
    sudo apt-get update -qq
    sudo apt-get install -y nginx
fi

if [[ ! -d "${LIBERTY_FS_ROOT}" ]]; then
    echo "==> Creating LIBERTY_FS_ROOT directory..."
    sudo mkdir -p "${LIBERTY_FS_ROOT}"
    sudo chown -R "${USER}:${USER}" "$(dirname "${LIBERTY_FS_ROOT}")"
fi

echo "==> Installing nginx site config..."
sudo cp "${SCRIPT_DIR}/${CONF_NAME}" "/etc/nginx/sites-available/${CONF_NAME}"

# Patch root path if LIBERTY_FS_ROOT differs from default
if [[ "${LIBERTY_FS_ROOT}" != "/home/user/publicfolder/liberty_fs" ]]; then
    sudo sed -i "s|root /home/user/publicfolder/liberty_fs;|root ${LIBERTY_FS_ROOT};|" \
        "/etc/nginx/sites-available/${CONF_NAME}"
fi

# Patch port if needed
if [[ "${LIBERTY_FS_PORT}" != "843" ]]; then
    sudo sed -i "s/listen 843;/listen ${LIBERTY_FS_PORT};/" \
        "/etc/nginx/sites-available/${CONF_NAME}"
    sudo sed -i "s/listen \\[::\\]:843;/listen [::]:${LIBERTY_FS_PORT};/" \
        "/etc/nginx/sites-available/${CONF_NAME}"
fi

sudo ln -sf "/etc/nginx/sites-available/${CONF_NAME}" "/etc/nginx/sites-enabled/${CONF_NAME}"

echo "==> Testing nginx configuration..."
sudo nginx -t

echo "==> Enabling and starting nginx..."
sudo systemctl enable nginx
sudo systemctl restart nginx

echo
echo "Done. Test with an existing screen, e.g.:"
echo "  curl -I http://localhost:${LIBERTY_FS_PORT}/loan-application-screen/"
echo
echo "Update backend/config.json:"
echo "  \"LIBERTY_FS_ROOT\": \"${LIBERTY_FS_ROOT}\""
echo "  \"LIBERTY_FS_BASE_URL\": \"http://localhost:${LIBERTY_FS_PORT}\""
