#!/usr/bin/env bash
# Restart a quest-ai-runner lane ONLY when it is idle, so a refresh can never kill an in-flight
# deep run.
#
# WHY THIS EXISTS
# A lane loads its code at process start. If you installed the library editable (`pip install -e`),
# or you edited your consumer, or you upgraded the package in place, the running lane keeps
# executing whatever was in memory at its last restart. That failure is silent and it is easy to
# lose days to: a deployment observed in the wild was still running five-day-old code after the
# fixes had landed, because nothing ever restarted the service.
#
# The obvious fix -- restart on a timer -- has its own failure: a deep run can take many minutes,
# and a blind `systemctl restart` in the middle of one throws that work away. This script is the
# safe version of the same idea, and is what `docs/deployment.md` ("Upgrading a running lane")
# tells you to schedule.
#
# IDLE TEST
# A deep run spawns a coding-agent child process inside the service cgroup (see
# `core/goal_runner.py`). Shallow work is just busy CPU on the main python process. We only check
# for the deep child, deliberately: shallow steps finish in seconds and survive a restart
# losslessly, because an unclaimed task stays queued and a task claimed mid-shallow-step is reaped
# by the backend's stale-task sweep. A deep run is the only thing worth protecting.
#
# USAGE
#   restart_if_idle.sh <service-name> [--system]
#
#   restart_if_idle.sh my-lane.service              # a `systemctl --user` unit (the default)
#   restart_if_idle.sh my-lane.service --system     # a system unit (needs privileges)
#
# Schedule it from a systemd timer or cron; see docs/deployment.md for both.
#
# EXIT CODES
#   0  restarted, or deliberately skipped because a deep run was in flight
#   2  bad usage

set -u

SERVICE="${1:?usage: restart_if_idle.sh <service-name> [--system]}"
SCOPE="${2:---user}"

case "$SCOPE" in
    --user|--system) ;;
    *) echo "usage: restart_if_idle.sh <service-name> [--system]" >&2; exit 2 ;;
esac

# Names of the deep-run child processes to look for. The deep runner spawns a coding-agent CLI;
# override for a consumer that wires a different DeepRunner with a differently-named child.
DEEP_CHILD_COMMS="${QAR_DEEP_CHILD_COMMS:-claude node}"

if [ "$SCOPE" = "--user" ]; then
    # Force the runtime dir to THIS user's. An inherited value from another user's shell (common
    # when a task process was spawned with a different user's environment) breaks the user-bus
    # connection with "Operation not permitted".
    export XDG_RUNTIME_DIR="/run/user/$(id -u)"
    CGROUP_DIR="/sys/fs/cgroup/user.slice/user-$(id -u).slice/user@$(id -u).service/app.slice/${SERVICE}"
else
    CGROUP_DIR="/sys/fs/cgroup/system.slice/${SERVICE}"
fi

if [ -f "${CGROUP_DIR}/cgroup.procs" ]; then
    while read -r pid; do
        [ -n "$pid" ] || continue
        comm="$(cat "/proc/${pid}/comm" 2>/dev/null || true)"
        for want in $DEEP_CHILD_COMMS; do
            if [ "$comm" = "$want" ]; then
                echo "${SERVICE}: deep run in flight (pid ${pid} ${comm}); skipping restart"
                exit 0
            fi
        done
    done < "${CGROUP_DIR}/cgroup.procs"
else
    # No cgroup file: either the unit is not running, or this kernel/layout differs. Restarting a
    # stopped unit is harmless, so continue rather than failing -- but say so, because on a live
    # unit it means the idle guard did not actually run.
    echo "${SERVICE}: no cgroup at ${CGROUP_DIR}; idle guard could not check for an in-flight deep run"
fi

echo "${SERVICE}: idle; restarting to pick up current code"
systemctl "$SCOPE" restart "${SERVICE}"
