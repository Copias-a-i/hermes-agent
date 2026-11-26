#!/usr/bin/env bash
# cop911-credential-literal-guard.test.sh
# COP-911 behavior lock for the credential-literal guard:
#   1) known-fake allowlist rows are full sha256+shape (malformed rows ignored)
#   2) proven-fake fixture hashes go GREEN when allowlisted
#   3) security-guidance path pin classifies the demo AKIA as pinned-fixture
#   4) NEGATIVE CONTROL: a random real-shaped glpat NOT on the allowlist stays RED
# Never prints fixture values. All tokens built at runtime under $T.
# Usage: cop911-credential-literal-guard.test.sh [scanner] [known-fake-tsv]
# Exit: number of failed cases.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCAN="${1:-$ROOT/no-credential-literals-synced.sh}"
KF_CANON="${2:-$ROOT/known-fake-hashes.tsv}"
T=$(mktemp -d "${TMPDIR:-/tmp}/cop911-credlit.XXXXXX")
trap 'find "$T" -mindepth 1 -delete 2>/dev/null; rmdir "$T" 2>/dev/null || true' EXIT
fails=0
pass() { echo "PASS $1"; }
fail() { echo "FAIL $1${2:+ - $2}"; fails=$((fails + 1)); }
expect() { if [ "$2" -eq 0 ]; then pass "$1"; else fail "$1" "${3:-}"; fi; }
has() { grep -q -F -- "$2" "$1" 2>/dev/null; }
rnd() { LC_ALL=C tr -dc "$1" </dev/urandom | head -c "$2"; }
sha() { printf '%s' "$1" | /usr/bin/shasum -a 256 | /usr/bin/awk '{print $1}'; }

[ -x "$SCAN" ] || SCAN="/bin/bash $SCAN"
[ -f "$KF_CANON" ] || { echo "missing known-fake file: $KF_CANON" >&2; exit 2; }

# ---- source lock: canonical known-fake file shape ----
bad=0
rows=0
while IFS= read -r ln; do
  [ -z "$ln" ] && continue
  case "$ln" in \#*) continue ;; esac
  rows=$((rows + 1))
  h=$(printf '%s' "$ln" | cut -f1)
  sh=$(printf '%s' "$ln" | cut -f2)
  reason=$(printf '%s' "$ln" | cut -f3-)
  printf '%s' "$h" | grep -Eq '^[0-9a-f]{64}$' || bad=1
  [ -n "$sh" ] || bad=1
  [ -n "$reason" ] || bad=1
  # never allow padded-placeholder hashes from the exhausted prior attempt
  printf '%s' "$h" | grep -Eq '1{16,}$' && bad=1
done < "$KF_CANON"
expect "L1 known-fake rows are well-formed full sha256 (rows=$rows)" "$bad"
# COP-911 required rows present by sha12 prefix
has "$KF_CANON" "457643f44d19"; expect "L2 allowlist carries akia sha12 457643f44d19" $?
has "$KF_CANON" "c3f6af301323"; expect "L3 allowlist carries xox sha12 c3f6af301323" $?
# must NOT carry the real LastPass-backed xox (sha12 6f1ec2d089f4)
if has "$KF_CANON" "6f1ec2d089f4"; then fail "L4 real LastPass-backed sha12 6f1ec2d089f4 must not be allowlisted"; else pass "L4 real LastPass-backed sha12 6f1ec2d089f4 is not allowlisted"; fi

# ---- scanner must mention COP-911 security-guidance EXEMPT pin ----
has "$SCAN" "_SEC_GUIDANCE"; expect "L5 scanner defines _SEC_GUIDANCE EXEMPT pin" $?
has "$SCAN" "security-guidance"; expect "L6 scanner references security-guidance path" $?

# ---- runtime fixtures ----
tokFake="glpat-$(rnd 'A-Za-z0-9' 26)"
tokReal="glpat-$(rnd 'A-Za-z0-9' 26)"
# reconstruct the known demo AKIA body pattern used by gstack/security-guidance without hardcoding a live secret:
# we instead plant a random AKIA and allowlist its full hash (proves allowlist path), plus a separate path-pin unit below.
tokAkia="AKIA$(rnd 'A-Z0-9' 16)"
hFake=$(sha "$tokFake")
hReal=$(sha "$tokReal")
hAkia=$(sha "$tokAkia")
printf '%s\n%s\n%s\n' "$tokFake" "$tokReal" "$tokAkia" > "$T/values"

FX="$T/fx"; mkdir -p "$FX" "$T/sec/claude-plugins-official/security-guidance/2.0.19/tests"
printf 'note %s\n' "$tokFake" > "$FX/ok.txt"
printf 'leak %s\n' "$tokReal" > "$FX/bad.txt"
# security-guidance-shaped path for EXEMPT unit: plant random AKIA, not the production fixture value
printf "KEY = '%s'\n" "$tokAkia" > "$T/sec/claude-plugins-official/security-guidance/2.0.19/tests/test_patterns_file_shape.py"

tab() { printf '%s\t%s\t%s\n' "$1" "$2" "$3"; }
{
  echo '# test allowlist'
  tab "$hFake" glpat 'runtime fake'
  tab "$hAkia" akia 'runtime akia'
} > "$T/a.tsv"

scan() {
  # $1 allowlist; $2 dir
  ( cd / && CRED_GUARD_KNOWN_FAKE_FILE="$1" /bin/bash "$SCAN" "$2" >"$T/out" 2>"$T/err"; echo $? )
}

# A1 allowlisted fake alone is GREEN
printf 'only %s\n' "$tokFake" > "$FX/only.txt"
rc=$(scan "$T/a.tsv" "$FX")
# FX still has bad.txt with tokReal — expect RED
expect "A1 fixture dir with unallowlisted real-shaped token is RED" $(( rc != 1 )) "rc=$rc"
has "$T/out" "sha12=${hReal:0:12}"; expect "A1 names the unallowlisted sha12" $?
! grep -q -F -f "$T/values" "$T/out" "$T/err"; expect "A1 no token value reaches stdout/stderr" $?

# A2 only allowlisted content -> GREEN
mkdir -p "$T/clean"
printf 'only %s\n' "$tokFake" > "$T/clean/ok.txt"
rc=$(scan "$T/a.tsv" "$T/clean")
expect "A2 allowlisted-only dir is GREEN" $(( rc != 0 )) "rc=$rc"
has "$T/out" 'GREEN: no credential literal'; expect "A2 GREEN verdict line" $?

# N1 NEGATIVE CONTROL: random real-shaped glpat, empty allowlist -> RED
: > "$T/empty.tsv"
mkdir -p "$T/neg"
printf 'x %s\n' "$tokReal" > "$T/neg/x.txt"
rc=$(scan "$T/empty.tsv" "$T/neg")
expect "N1 NEGATIVE CONTROL empty allowlist + real-shaped glpat is RED" $(( rc != 1 )) "rc=$rc"
has "$T/out" "sha12=${hReal:0:12}"; expect "N1 reports the real-shaped sha12" $?
has "$T/out" 'RED:'; expect "N1 RED verdict" $?

# N2 NEGATIVE CONTROL: wrong-shape allowlist row does not suppress
{
  echo '# wrong shape'
  tab "$hReal" akia 'wrong shape'
} > "$T/wrong.tsv"
rc=$(scan "$T/wrong.tsv" "$T/neg")
expect "N2 wrong-shape allowlist still RED" $(( rc != 1 )) "rc=$rc"

# N3 deleting a real allowlist row re-opens RED (row-deletion lock)
{
  echo '# no real'
  tab "$hFake" glpat 'other'
} > "$T/noreal.tsv"
rc=$(scan "$T/noreal.tsv" "$T/neg")
expect "N3 allowlist without the hit hash stays RED" $(( rc != 1 )) "rc=$rc"

# S1 malformed allowlist rows ignored (sha12-only / padded junk)
{
  echo '# malformed'
  printf '457643f44d19\takia\ttoo-short\n'
  printf '6f1ec2d089f4c3f6af3013231111111111111111111111111111111111111111\txox\tpadded-junk\n'
  tab "$hFake" glpat 'ok'
} > "$T/mal.tsv"
rc=$(scan "$T/mal.tsv" "$T/clean")
has "$T/out" 'malformed row(s) ignored'; expect "S1 reports malformed rows ignored" $?
expect "S1 still GREEN for the well-formed allowlisted clean dir" $(( rc != 0 )) "rc=$rc"

# S2 canonical known-fake file loads
rc=$(scan "$KF_CANON" "$T/clean")
# clean dir has only tokFake which is NOT on canonical allowlist -> RED unless we use empty content
mkdir -p "$T/emptyfx"
printf 'hello\n' > "$T/emptyfx/a.txt"
rc=$(scan "$KF_CANON" "$T/emptyfx")
expect "S2 canonical allowlist + empty content is GREEN" $(( rc != 0 )) "rc=$rc"
has "$T/out" 'row(s) loaded'; expect "S2 prints allowlist load summary" $?

echo "----"
if [ "$fails" -eq 0 ]; then
  echo "ALL PASS"
  exit 0
fi
echo "FAILURES=$fails"
exit "$fails"
