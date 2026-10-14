#!/bin/sh
set -eu

[ "$(id -u)" -eq 0 ] || { echo "Run legacy service retirement as root" >&2; exit 1; }
backup_root=/var/lib/bibit-intent/retired-legacy
mkdir -p "$backup_root"
chmod 700 "$backup_root"
backup=$(mktemp -d "$backup_root/migration.XXXXXX")

retire_file() {
    source=$1
    if [ -e "$source" ] || [ -L "$source" ]; then
        destination="$backup$source"
        mkdir -p "$(dirname "$destination")"
        mv "$source" "$destination"
    fi
}

retire_units() {
    directory=$1
    for name in bibit-intent-llm.service bibit-intent-embed.service; do
        retire_file "$directory/$name"
        retire_file "$directory/$name.d"
        if [ -d "$directory" ]; then
            find "$directory" -type l -name "$name" -delete
        fi
    done
}

if [ -d /run/systemd/system ]; then
    for unit in bibit-intent-llm.service bibit-intent-embed.service; do
        state=$(systemctl show -p LoadState --value "$unit" 2>/dev/null || true)
        if [ -n "$state" ] && [ "$state" != not-found ]; then
            systemctl stop "$unit"
        fi
        systemctl disable "$unit" >/dev/null 2>&1 || true
        systemctl --global disable "$unit" >/dev/null 2>&1 || true
    done
fi

getent passwd | while IFS=: read -r username unused uid gid description home shell; do
    if [ "$uid" -ne 0 ] && { [ "$uid" -lt 1000 ] || [ "$uid" -eq 65534 ]; }; then
        continue
    fi
    if [ -S "/run/user/$uid/bus" ]; then
        for unit in bibit-intent-llm.service bibit-intent-embed.service; do
            state=$(runuser -u "$username" -- env XDG_RUNTIME_DIR="/run/user/$uid" systemctl --user show -p LoadState --value "$unit" 2>/dev/null || true)
            if [ -n "$state" ] && [ "$state" != not-found ]; then
                runuser -u "$username" -- env XDG_RUNTIME_DIR="/run/user/$uid" systemctl --user stop "$unit"
            fi
            runuser -u "$username" -- env XDG_RUNTIME_DIR="/run/user/$uid" systemctl --user disable "$unit" >/dev/null 2>&1 || true
        done
    fi
    retire_units "$home/.config/systemd/user"
    if [ -S "/run/user/$uid/bus" ]; then
        runuser -u "$username" -- env XDG_RUNTIME_DIR="/run/user/$uid" systemctl --user daemon-reload
    fi
done

retire_units /etc/systemd/system
retire_units /etc/systemd/user
retire_units /usr/lib/systemd/user
for name in select-model.sh model-priority.conf download_llm_model.sh download_embed_model.sh install_llama_cpp_spacemit.sh; do
    retire_file "/opt/bibit-intent/$name"
    retire_file "/opt/bibit-intent/deploy/$name"
done
for name in bibit-intent-llm.service bibit-intent-embed.service; do
    retire_file "/opt/bibit-intent/deploy/$name"
done
retire_file /opt/bibit-intent/src/bibit_intent/models/model_manager.py
retire_file /opt/bibit-intent/src/bibit_intent/models/model_manager.pyc
for bytecode in /opt/bibit-intent/src/bibit_intent/models/__pycache__/model_manager.*.pyc; do
    retire_file "$bytecode"
done
if [ -d /run/systemd/system ]; then
    systemctl daemon-reload
    getent passwd | while IFS=: read -r username unused uid gid description home shell; do
        if [ -S "/run/user/$uid/bus" ]; then
            runuser -u "$username" -- env XDG_RUNTIME_DIR="/run/user/$uid" systemctl --user daemon-reload
        fi
    done
fi
echo "Legacy model units retired; model files preserved. Backup: $backup"
