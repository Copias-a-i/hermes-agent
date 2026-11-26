#!/usr/bin/env bash
# credential-literal-allowlist-summary.test.sh - behaviour lock for the known-fake allowlist summary of
# no-credential-literals-synced.sh (COP-706 follow-up, 2026-10-13).
#   "21 hit(s) allowlisted" next to "6 row(s) loaded" read as 21 distinct secrets against 6 rows. It was 21
#   OCCURRENCES of 6 hashes (one placeholder quoted in many transcripts / .stversions copies / memory rows).
#   The summary now says "N occurrence(s) of M allowlisted hash(es)" and one line per hash names its row, shape,
#   sha12, occurrences and files. This test pins that, and that an allowlisted hit can only come from a listed row.
# Nothing here touches the real synced dirs or the real known-fake file: every case scans a throwaway directory
# with a throwaway allowlist (CRED_GUARD_KNOWN_FAKE_FILE). Fixture values are random, built at run time, never
# printed and never written outside $T, so this file holds no token-shaped literal.
# Usage: credential-literal-allowlist-summary.test.sh [scanner]   (default: ~/bin/tests/no-credential-literals-synced.sh)
# Exit: number of failed cases.
set -u
SCAN="${1:-$HOME/bin/tests/no-credential-literals-synced.sh}"
T=$(mktemp -d "${TMPDIR:-/tmp}/credlit-test.XXXXXX"); trap 'find "$T" -mindepth 1 -delete 2>/dev/null; rmdir "$T" 2>/dev/null || true' EXIT
fails=0
pass() { echo "PASS $1"; }
fail() { echo "FAIL $1${2:+ - $2}"; fails=$((fails + 1)); }
expect() { if [ "$2" -eq 0 ]; then pass "$1"; else fail "$1" "${3:-}"; fi; }
has() { grep -q -F -- "$2" "$1" 2>/dev/null; }
rnd() { LC_ALL=C tr -dc "$1" </dev/urandom | head -c "$2"; }
sha() { printf '%s' "$1" | /usr/bin/shasum -a 256 | /usr/bin/awk '{print $1}'; }

# ---- fixtures: three token-shaped values (A twice+once, B once, C once) and a fourth hash that is in no file ------
tokA="glpat-$(rnd 'A-Za-z0-9' 26)"; tokB="AKIA$(rnd 'A-Z0-9' 16)"; tokC="glpat-$(rnd 'A-Za-z0-9' 26)"; tokD="glpat-$(rnd 'A-Za-z0-9' 26)"
hA=$(sha "$tokA"); hB=$(sha "$tokB"); hC=$(sha "$tokC"); hD=$(sha "$tokD")
printf '%s\n%s\n%s\n' "$tokA" "$tokB" "$tokC" > "$T/values"      # to prove no output ever carries a value
FX="$T/fx"; mkdir -p "$FX"
printf 'one %s and again %s\n' "$tokA" "$tokA" > "$FX/f1.txt"
printf 'note %s\n' "$tokA" > "$FX/f2.md"
printf '{"k": "%s"}\n' "$tokB" > "$FX/f3.json"
printf 'c = %s\n' "$tokC" > "$FX/f4.txt"
tab() { printf '%s\t%s\t%s\n' "$1" "$2" "$3"; }
mktsv() { # $1 = path; rows: A glpat, B akia, C glpat, D glpat (idle: its value is in no file)
  { echo '# test allowlist'; tab "$hA" glpat 'fixture A'; tab "$hB" akia 'fixture B'; tab "$hC" glpat 'fixture C'; tab "$hD" glpat 'idle row'; } > "$1"
}
scan() { # $1 = allowlist path; env: EXPLAIN. Prints the scanner's exit code; output in $T/out, $T/err.
  ( cd / && CRED_GUARD_KNOWN_FAKE_FILE="$1" CRED_GUARD_EXPLAIN="${EXPLAIN:-}" /bin/bash "$SCAN" "$FX" >"$T/out" 2>"$T/err"; echo $? )
}

# S1 every hit is a listed row: GREEN, and the line says occurrences of hashes (5 occurrences of 3 hashes, 4 rows)
mktsv "$T/a.tsv"; rc=$(scan "$T/a.tsv")
expect "S1 all hits allowlisted exits 0" $(( rc != 0 )) "rc=$rc"
has "$T/out" '4 row(s) loaded, 0 malformed row(s) ignored, 5 occurrence(s) of 3 allowlisted hash(es) ('; expect "S1 summary reads 'N occurrence(s) of M allowlisted hash(es)'" $?
! has "$T/out" 'hit(s) allowlisted'; expect "S1 the ambiguous 'hit(s) allowlisted' wording is gone" $?
has "$T/out" "  allowlisted row=2 shape=glpat sha12=${hA:0:12} occurrences=3 files=2"; expect "S1 hash A: row 2, 3 occurrences in 2 files" $?
has "$T/out" "  allowlisted row=3 shape=akia sha12=${hB:0:12} occurrences=1 files=1"; expect "S1 hash B: row 3, 1 occurrence in 1 file" $?
has "$T/out" "  allowlisted row=4 shape=glpat sha12=${hC:0:12} occurrences=1 files=1"; expect "S1 hash C: row 4, 1 occurrence in 1 file" $?
has "$T/out" '  allowlist rows with no hit in this scan: row=5 shape=glpat'; expect "S1 the idle row (row 5) is named" $?
tail -n 1 "$T/out" | grep -q '^GREEN: no credential literal in 1 scanned dir(s)'; expect "S1 the last line is still the GREEN verdict" $?
n=$(sed -n 's/.* \([0-9][0-9]*\) occurrence(s) of .*/\1/p' "$T/out"); m=$(sed -n 's/.* occurrence(s) of \([0-9][0-9]*\) allowlisted.*/\1/p' "$T/out")
sum=$(sed -n 's/^  allowlisted row=.* occurrences=\([0-9][0-9]*\) files=.*/\1/p' "$T/out" | awk '{s+=$1} END {print s+0}'); lines=$(grep -c '^  allowlisted row=' "$T/out")
expect "S1 N equals the sum of the per-hash occurrences" $(( n != sum )) "N=$n sum=$sum"
expect "S1 M equals the number of per-hash lines" $(( m != lines )) "M=$m lines=$lines"
bad=0; seen=0
while IFS=' ' read -r _ prow pshape psha _; do   # "  allowlisted row=2 shape=glpat sha12=... occurrences=3 files=2"
  r=${prow#row=}; sh=${pshape#shape=}; h=${psha#sha12=}; seen=$((seen + 1))
  line=$(sed -n "${r}p" "$T/a.tsv")
  { [ "${line:0:12}" = "$h" ] && [ "$(printf '%s' "$line" | cut -f2)" = "$sh" ]; } || bad=1
done <<< "$(grep '^  allowlisted row=' "$T/out")"
expect "S1 every allowlisted hit maps to the row it names (that line of the allowlist file starts with the same sha12 and shape)" $(( bad || seen != 3 )) "seen=$seen"
! grep -q -F -f "$T/values" "$T/out" "$T/err"; expect "S1 no token value reaches stdout or stderr" $?

# S2 EXPLAIN=1 names every file with its per-file count (opt-in; default output stays short)
rc=$(EXPLAIN=1 scan "$T/a.tsv")
has "$T/out" "    in $FX/f1.txt count=2"; expect "S2 EXPLAIN lists f1.txt with count=2" $?
has "$T/out" "    in $FX/f2.md count=1"; expect "S2 EXPLAIN lists f2.md with count=1" $?
rc=$(scan "$T/a.tsv"); ! has "$T/out" '    in '; expect "S2 default output carries no per-file lines" $?

# S3 negative control: delete hash A's row -> its 3 occurrences are findings again, B and C stay allowlisted
{ echo '# test allowlist'; tab "$hB" akia 'fixture B'; tab "$hC" glpat 'fixture C'; tab "$hD" glpat 'idle row'; } > "$T/c.tsv"; rc=$(scan "$T/c.tsv")
expect "S3 a deleted row makes the scan RED (rc 1)" $(( rc != 1 )) "rc=$rc"
has "$T/out" "FAIL credential literal in: $FX/f1.txt shape=glpat count=2 sha12=${hA:0:12}"; expect "S3 f1.txt is a FAIL with its 2 occurrences" $?
has "$T/out" 'RED: 3 credential literal(s) in 2 file(s)'; expect "S3 RED counts 3 literals in 2 files" $?
has "$T/out" '3 row(s) loaded, 0 malformed row(s) ignored, 2 occurrence(s) of 2 allowlisted hash(es) ('; expect "S3 and the summary shrinks to 2 occurrences of 2 hashes" $?

# S4 the hash with the WRONG shape is not allowlisted (the key is hash AND shape)
{ echo '# test allowlist'; tab "$hA" akia 'wrong shape'; tab "$hB" akia 'fixture B'; tab "$hC" glpat 'fixture C'; } > "$T/d.tsv"; rc=$(scan "$T/d.tsv")
expect "S4 right hash + wrong shape stays RED" $(( rc != 1 )) "rc=$rc"
has "$T/out" "sha12=${hA:0:12}"; expect "S4 and names the unallowlisted hash" $?

# S5 malformed rows are ignored and counted, valid rows still work
{ mktsv "$T/e0.tsv"; cat "$T/e0.tsv"; echo 'deadbeef	glpat	short hash'; printf '%s\t%s\t\n' "$hA" glpat; } > "$T/e.tsv"; rc=$(scan "$T/e.tsv")
expect "S5 valid rows still allowlist with malformed rows present" $(( rc != 0 )) "rc=$rc"
has "$T/out" '4 row(s) loaded, 2 malformed row(s) ignored, 5 occurrence(s) of 3 allowlisted hash(es) ('; expect "S5 malformed rows are counted" $?

# S6 no allowlist file at all: nothing is allowlisted, the summary says so
rc=$(scan "$T/does-not-exist.tsv")
expect "S6 a missing allowlist file is RED (stricter)" $(( rc != 1 )) "rc=$rc"
has "$T/out" '0 row(s) loaded, 0 malformed row(s) ignored, 0 occurrence(s) of 0 allowlisted hash(es) ('; expect "S6 summary says 0 occurrence(s) of 0 hash(es)" $?
has "$T/out" 'RED: 5 credential literal(s) in 4 file(s)'; expect "S6 all 5 occurrences are findings" $?

[ "$fails" -eq 0 ] && echo "ALL PASS" || echo "$fails FAILED"
exit "$fails"
