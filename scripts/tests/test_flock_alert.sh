#!/usr/bin/env bash
# Q3 TDD test: flock_guard.sh enhanced skipped_lock alert (card #40)
set -u

TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT

mkdir -p "$TMPDIR/scripts" "$TMPDIR/locks" "$TMPDIR/logs"
cp "${FLOCK_SRC:-${QS_HOME:-$HOME/qs}/scripts/flock_guard.sh}" "$TMPDIR/scripts/flock_guard.sh"
chmod +x "$TMPDIR/scripts/flock_guard.sh"

LOCK="$TMPDIR/locks/qs_db_write.lock"
touch "$LOCK"

# Hold the lock with a background process
(
    exec 9>"$LOCK"
    flock -x 9
    sleep 15
) &
HOLDER_PID=$!
sleep 1

export QS_WATCHDOG_LOG="$TMPDIR/logs/watchdog.jsonl"
STDERR_OUT=$("$TMPDIR/scripts/flock_guard.sh" test_job_key echo "should_not_run" 2>&1 >/dev/null)
RC=$?

kill $HOLDER_PID 2>/dev/null || true
wait $HOLDER_PID 2>/dev/null || true

FAILURES=0

# Check 1: exit code 99
if [ "$RC" -ne 99 ]; then
    echo "FAIL[1]: expected RC=99, got $RC"; FAILURES=$((FAILURES+1))
else
    echo "PASS[1]: RC=99"
fi

# Check 2: stderr contains WARNING
if echo "$STDERR_OUT" | grep -qi "WARNING"; then
    echo "PASS[2]: WARNING level in stderr"
else
    echo "FAIL[2]: no WARNING in stderr: $STDERR_OUT"; FAILURES=$((FAILURES+1))
fi

# Check 3: stderr contains hold duration (Ns pattern)
if echo "$STDERR_OUT" | grep -qE "[0-9]+s"; then
    echo "PASS[3]: hold duration in stderr"
else
    echo "FAIL[3]: no hold duration in stderr: $STDERR_OUT"; FAILURES=$((FAILURES+1))
fi

# Check 4: watchdog.jsonl event has lock_owner_pid
if [ -f "$TMPDIR/logs/watchdog.jsonl" ]; then
    LAST_EVENT=$(tail -1 "$TMPDIR/logs/watchdog.jsonl")
    if echo "$LAST_EVENT" | grep -q "lock_owner_pid"; then
        echo "PASS[4]: lock_owner_pid in event"
    else
        echo "FAIL[4]: no lock_owner_pid in event: $LAST_EVENT"; FAILURES=$((FAILURES+1))
    fi
else
    echo "FAIL[4]: watchdog.jsonl not created"; FAILURES=$((FAILURES+1))
fi

echo ""
if [ $FAILURES -eq 0 ]; then
    echo "ALL PASS (4/4)"; exit 0
else
    echo "FAILURES: $FAILURES/4"; exit 1
fi
