#!/usr/bin/env bash
# Generate local-only passwords for svc-sim. Files are mode 0600 and their values are never printed.
set -euo pipefail
cd "$(dirname "$0")/../.."
umask 077
mkdir -p ops/secrets
chmod 700 ops/secrets
for name in agent_memory_password agent_working_memory_password shop_ro_password flagd_ui_secret_key_base; do
  f="ops/secrets/$name"
  if [ ! -s "$f" ]; then
    len=32; [ "$name" = flagd_ui_secret_key_base ] && len=64
    python3 -c "import secrets,string,sys; a=string.ascii_letters+string.digits; sys.stdout.write(''.join(secrets.choice(a) for _ in range($len)))" > "$f"
    chmod 600 "$f"
    echo "generated $f"
  fi
done
# Plain Compose bind-mounts secret files with host permissions, and the consuming containers run as
# different uids (svc-sim 10001, postgres 999, valkey 999). Files mounted into containers are therefore
# 0644 inside a 0700 directory: no other host user can traverse ops/secrets. The flagd-ui key is read
# only by make on the host and stays 0600.
chmod 644 ops/secrets/agent_memory_password ops/secrets/agent_working_memory_password ops/secrets/shop_ro_password
chmod 600 ops/secrets/flagd_ui_secret_key_base
echo "secrets ready (values not shown)"
