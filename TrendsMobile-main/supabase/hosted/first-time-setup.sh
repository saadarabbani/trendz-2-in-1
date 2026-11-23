#!/bin/bash
# Covison ERP - first-time setup for hosted Supabase, for macOS/Linux. Run this ONCE on your own computer.
# (Same steps as first-time-setup.ps1 for Windows.)
#
#   1. Open Terminal in the project folder (the one that contains "offline" and "supabase").
#   2. Run:   bash supabase/hosted/first-time-setup.sh
#   3. Answer the questions. Secrets are typed hidden; nothing is saved in the project or sent to GitHub,
#      except the private file supabase/private/vercel-env.txt, which git ignores.
#
# It creates the database tables and the sign-in owner account, and writes the values to paste into Vercel.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
APP="$ROOT/offline"
[ -f "$APP/manage.py" ] || { echo "Cannot find offline/manage.py. Run this from the project folder." >&2; exit 1; }

fail() { echo "ERROR: $*" >&2; exit 1; }
ask() { local v; read -r -p "$1: " v; printf '%s' "$(echo "$v" | xargs)"; }
ask_secret() { local v; read -r -s -p "$1 (typing is hidden): " v; echo >&2; printf '%s' "$(echo "$v" | xargs)"; }

# Prints the "role" and "ref" claims of an older eyJ... key, or nothing if it is not in that format.
jwt_claim() {
  python3 - "$1" "$2" <<'PY' 2>/dev/null || true
import base64, json, sys
try:
    p = sys.argv[1].split('.')[1]; p += '=' * (-len(p) % 4)
    print(json.loads(base64.urlsafe_b64decode(p)).get(sys.argv[2], ''))
except Exception:
    pass
PY
}

echo
echo "Step 1 of 3 - answers from your Supabase dashboard"
echo "Project URL: Project Settings > API. It looks like https://abcdefghijklmnop.supabase.co"
URL="$(ask "Project URL")"; URL="${URL%/}"
[[ "$URL" =~ ^https://([a-z0-9]+)\.supabase\.co$ ]] || fail "That does not look like https://<letters>.supabase.co"
REF="${BASH_REMATCH[1]}"

echo
echo "Pooler host: click the green 'Connect' button at the top of Supabase > 'Transaction pooler'."
echo "The host looks like aws-0-ap-south-1.pooler.supabase.com (copy only that part, no postgresql:// and no :6543)."
POOLER="$(ask "Pooler host")"
[[ "$POOLER" =~ ^[a-z0-9.-]+\.pooler\.supabase\.com$ ]] || fail "That does not look like xxxx.pooler.supabase.com"

echo
echo "The password you put in setup.sql for the covison_app user (letters and digits only)."
DB_PASSWORD="$(ask_secret "Database password")"
[[ "$DB_PASSWORD" =~ ^[A-Za-z0-9]{12,}$ ]] || fail "Use only letters and digits, 12 or more characters."

echo
echo "Keys: Project Settings > API Keys. Use the 'Legacy' tab: anon key and service_role key."
ANON="$(ask_secret "anon key")"
SERVICE="$(ask_secret "service_role key")"
[ ${#ANON} -ge 30 ] && [ ${#SERVICE} -ge 30 ] || fail "A key looks too short. Copy the whole key."
[ "$ANON" != "$SERVICE" ] || fail "The anon key and the service_role key are identical. Copy each from its own row."
for pair in "anon:$ANON" "service_role:$SERVICE"; do
  name="${pair%%:*}"; key="${pair#*:}"
  role="$(jwt_claim "$key" role)"; kref="$(jwt_claim "$key" ref)"
  if [ -n "$role" ]; then
    [ "$role" = "$name" ] || fail "The key you pasted as '$name' is actually a '$role' key. Swap them."
    [ -z "$kref" ] || [ "$kref" = "$REF" ] || fail "The $name key belongs to a different Supabase project ($kref), not $REF."
  else
    echo "Note: the $name key is not in the older eyJ... format, so it could not be pre-checked."
  fi
done

echo
echo "Step 2 of 3 - the owner login you will use to sign in to the ERP"
OWNER_EMAIL="$(ask "Owner email")"
[[ "$OWNER_EMAIL" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]] || fail "That is not a valid email address."
OWNER_PASSWORD="$(ask_secret "Owner password, 12+ characters")"
[ ${#OWNER_PASSWORD} -ge 12 ] || fail "Use at least 12 characters."

PYTHON=""
for v in 3.12 3.11 3.13; do command -v "python$v" >/dev/null 2>&1 && { PYTHON="python$v"; break; }; done
[ -n "$PYTHON" ] || fail "Python 3.11 or 3.12 was not found. Install Python 3.12 and run this again."

echo
echo "Step 3 of 3 - creating tables and the owner (takes a few minutes the first time)"
cd "$APP"
[ -d .venv ] || "$PYTHON" -m venv .venv || fail "Could not create the Python environment."
PY="$APP/.venv/bin/python"
"$PY" -m pip install --quiet --disable-pip-version-check -r requirements.txt || fail "Installing the requirements failed."

# These variables exist only for the commands below, never in your shell.
run() {
  DJANGO_SECRET_KEY='one-time-setup-value' \
  DATABASE_URL="postgresql://covison_app.$REF:$DB_PASSWORD@$POOLER:5432/postgres" \
  SUPABASE_URL="$URL" SUPABASE_PUBLISHABLE_KEY="$ANON" SUPABASE_SECRET_KEY="$SERVICE" \
  COVISON_CRM_OWNER_EMAIL="$OWNER_EMAIL" COVISON_CRM_OWNER_PASSWORD="$OWNER_PASSWORD" \
  "$PY" manage.py "$@"
}
echo "Creating tables..."
run migrate --noinput || fail "migrate failed. Check the database password and pooler host."
echo "Creating the sign-in rate-limit table..."
run createcachetable || fail "createcachetable failed."
echo "Creating the owner account..."
run bootstrap_supabase_owner || fail "Creating the owner failed. Check the Project URL and the service_role key."

# Values for Vercel go in a private file that git ignores (supabase/private/).
PRIVATE="$ROOT/supabase/private"; mkdir -p "$PRIVATE"; chmod 700 "$PRIVATE"
# (Not "tr </dev/urandom | head": under "set -o pipefail" tr dies of SIGPIPE and the script would stop silently here.)
randstr() { python3 -c 'import secrets,string,sys;a=string.ascii_letters+string.digits;print("".join(secrets.choice(a) for _ in range(int(sys.argv[1]))))' "$1"; }
SECRET="$(randstr 64)"
ONLINE_KEY="$(randstr 48)"
FILE="$PRIVATE/vercel-env.txt"
umask 077
cat >"$FILE" <<EOF
Paste these into Vercel > your ERP project > Settings > Environment Variables (one row each: Name, then Value).
KEEP THIS FILE PRIVATE. Delete it when you are done.

DJANGO_SECRET_KEY=$SECRET
COVISON_HTTPS=1
DATABASE_URL=postgresql://covison_app.$REF:$DB_PASSWORD@$POOLER:6543/postgres
SUPABASE_URL=$URL
SUPABASE_PUBLISHABLE_KEY=$ANON
SUPABASE_SECRET_KEY=$SERVICE
ONLINE_API_KEY=$ONLINE_KEY

The website (mobileshop-ecom) later needs the SAME ONLINE_API_KEY value as its ERP_API_KEY.
EOF

echo
echo "DONE. Your database and owner login are ready."
echo "Next: open this file, then follow 'Part 3 - Vercel' in DEPLOYMENT.md:"
echo "  $FILE"
open -e "$FILE" 2>/dev/null || true
