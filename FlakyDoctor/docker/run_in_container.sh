#!/usr/bin/env bash
# Run one AgentFlake OD container through FlakyDoctor + Claude
# inside the Illinois `testorder` Surefire environment, so even same-class OD
# pairs reproduce deterministically.
#
# Usage (from the FlakyDoctor root):
#   docker/run_in_container.sh <result_container> [extra run_af_fd args...]
#
# Examples:
#   docker/run_in_container.sh ormlitecore59309e5
#   docker/run_in_container.sh shardingsphereelasticjobelasticjoblitecore4b9afa4   # same-class — works under testorder
#   docker/run_in_container.sh ormlitecore59309e5 --skip-repair                    # reproduce only, no API cost
#
# What it does:
#   1. looks up the row in test_config.csv (in the FlakyDoctor root) to read its Java version
#   2. builds the matching image (flakydoctor-od8 / flakydoctor-od11) once
#   3. runs the container as your host UID/GID with FlakyDoctor bind-mounted, and
#      invokes  src/run_af_fd.py --testorder --container <id> --model OpenAI
set -euo pipefail

CONTAINER="${1:-}"
shift || true
if [[ -z "$CONTAINER" ]]; then
    echo "usage: $0 <result_container> [extra run_af_fd args...]" >&2
    exit 1
fi

# Resolve paths: this script lives in FlakyDoctor/docker/.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FLAKYDOCTOR_DIR="$(dirname "$SCRIPT_DIR")"
TEST_CONFIG="${TEST_CONFIG:-$FLAKYDOCTOR_DIR/test_config.csv}"
RUN_MODEL="${FD_RUN_MODEL:-OpenAI}"
if [[ "$RUN_MODEL" == "Claude" ]]; then
    API_KEY_ENV_NAME="ANTHROPIC_API_KEY"
    API_KEY_FILE="${API_KEY_FILE:-$FLAKYDOCTOR_DIR/.anthropic_api_key}"
else
    API_KEY_ENV_NAME="OPENAI_API_KEY"
    API_KEY_FILE="${API_KEY_FILE:-$FLAKYDOCTOR_DIR/.openai_api_key}"
fi

# --skip-repair reproduces the flake only and never calls the API, so it needs no
# key. Detect it among the passthrough args.
SKIP_REPAIR="false"
for _a in "$@"; do [[ "$_a" == "--skip-repair" ]] && SKIP_REPAIR="true"; done

# Resolve the API key: provider env wins, else the provider-specific key file.
# Required only for an actual repair run (matches runner/run_claude.py's behavior).
API_KEY="${!API_KEY_ENV_NAME:-}"
if [[ -z "$API_KEY" && -f "$API_KEY_FILE" ]]; then
    API_KEY="$(cat "$API_KEY_FILE")"
fi
if [[ -z "$API_KEY" ]]; then
    if [[ "$SKIP_REPAIR" == "true" ]]; then
        API_KEY="unused"   # never sent to the API in --skip-repair mode
    else
        echo "no API key: set $API_KEY_ENV_NAME=... or create $API_KEY_FILE (or pass --skip-repair)" >&2
        exit 1
    fi
fi

[[ -f "$TEST_CONFIG" ]]   || { echo "test_config.csv not found at $TEST_CONFIG" >&2; exit 1; }
command -v docker >/dev/null 2>&1 || { echo "docker not installed — see docker/README_af_fd_od.md" >&2; exit 1; }

# Read the row's test_type (col 1), module (col 4), and Java version (col 9)
# for the container (col 2).
ROW_INFO="$(awk -F, -v c="$CONTAINER" '$2==c {print tolower($1)"\t"$4"\t"$9; exit}' "$TEST_CONFIG")"
if [[ -z "$ROW_INFO" ]]; then
    echo "no row with result_container == $CONTAINER in $TEST_CONFIG" >&2
    echo "tip: src/run_af_fd.py --list (OD)  |  src/run_af_fd_id.py --list (ID)" >&2
    exit 1
fi
IFS=$'\t' read -r TEST_TYPE MODULE JAVA_VER <<< "$ROW_INFO"

# OD, ID, NIO and TD share the same image. Pick the driver + mode flags by test_type.
# For ID, NIO and TD we must DISABLE the Illinois testorder Surefire extension: it lives in
# /usr/share/maven/lib/ext and forces maven-surefire-plugin:3.0.0-M8-SNAPSHOT into
# every build, which the offline .m2 doesn't have (ID is built with NonDex; NIO and TD with
# stock Surefire, not testorder) -> build failure. An empty tmpfs over that dir hides the
# extension so Maven uses the project's own surefire. OD keeps it (testorder needed).
EXTRA_RUN_ARGS=()
WITH_TESTORDER="true"   # OD needs the Illinois testorder Surefire; ID, NIO and TD never do
case "$TEST_TYPE" in
    od) DRIVER="src/run_af_fd.py";    MODE_ARGS="--testorder" ;;
    id) DRIVER="src/run_af_fd_id.py"; MODE_ARGS=""; WITH_TESTORDER="false"
        EXTRA_RUN_ARGS+=(--tmpfs /usr/share/maven/lib/ext) ;;
    nio) DRIVER="src/run_af_fd_nio.py"; MODE_ARGS=""; WITH_TESTORDER="false"
        EXTRA_RUN_ARGS+=(--tmpfs /usr/share/maven/lib/ext) ;;
    td) DRIVER="src/run_af_fd_td.py"; MODE_ARGS=""; WITH_TESTORDER="false"
        EXTRA_RUN_ARGS+=(--tmpfs /usr/share/maven/lib/ext) ;;
    *)  echo "unsupported test_type '$TEST_TYPE' for $CONTAINER (only od, id, nio and td are wired)" >&2; exit 1 ;;
esac

# Base image per JDK. JDK 17 has no maven:3.8.6-openjdk-17 tag and the old
# Surefire fork doesn't compile on 17, so it uses temurin-17 with no testorder.
case "$JAVA_VER" in
    17) IMAGE="flakydoctor-od17"; BASE="maven:3.8.6-eclipse-temurin-17"; WITH_TESTORDER="false" ;;
    11) IMAGE="flakydoctor-od11"; BASE="maven:3.8.6-openjdk-11" ;;
    8)  IMAGE="flakydoctor-od8";  BASE="maven:3.8.6-openjdk-8"  ;;
    *)  echo "row asks for Java $JAVA_VER; only 8, 11, 17 are wired." >&2
        exit 1 ;;
esac
if [[ "$TEST_TYPE" == "od" && "$WITH_TESTORDER" == "false" ]]; then
    echo "OD on Java $JAVA_VER needs the testorder Surefire, which isn't built for this JDK." >&2
    exit 1
fi

# ID on JDK 8/11 uses a no-testorder image (~3 min build vs ~20; ID never uses
# testorder). The od17 image is already no-testorder.
if [[ "$WITH_TESTORDER" == "false" && "$JAVA_VER" != "17" ]]; then
    IMAGE="${IMAGE}-noto"
fi

WITH_PROTOBUF="false"
MODULE_KEY="$(printf '%s\n' "$MODULE" | tr '[:upper:]' '[:lower:]')"
if [[ "$MODULE_KEY" == *hadoop* ]]; then
    WITH_PROTOBUF="true"
    IMAGE="${IMAGE}-hadoop"
fi

echo "[af_fd] container=$CONTAINER  type=$TEST_TYPE  java=$JAVA_VER  image=$IMAGE  driver=$DRIVER"

DOCKER_PLATFORM_ARGS=()
if [[ -n "${AGENTIC_DOCKER_PLATFORM:-}" ]]; then
    DOCKER_PLATFORM_ARGS=(--platform "$AGENTIC_DOCKER_PLATFORM")
elif [[ "$(uname -s)" == "Darwin" && "$(uname -m)" == "arm64" ]]; then
    DOCKER_PLATFORM_ARGS=(--platform linux/amd64)
fi

# Build the image once. On Apple Silicon, force a build when --platform is set:
# a same-named local image may exist only for the host architecture, which makes
# docker run --platform linux/amd64 try to pull the private local tag.
if ((${#DOCKER_PLATFORM_ARGS[@]})) || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "[af_fd] building $IMAGE from $BASE (one-time, WITH_TESTORDER=$WITH_TESTORDER) ..."
    docker build "${DOCKER_PLATFORM_ARGS[@]}" -t "$IMAGE" \
        --build-arg "BASE=$BASE" \
        --build-arg "WITH_TESTORDER=$WITH_TESTORDER" \
        --build-arg "WITH_PROTOBUF=$WITH_PROTOBUF" \
        -f "$FLAKYDOCTOR_DIR/docker/Dockerfile.flakydoctor_od" "$FLAKYDOCTOR_DIR"
fi

# Run as the host user so projects/ and outputs/ are owned by you (not root),
# and so git inside the bind-mounted repo sees a matching owner (no "dubious
# ownership"). HOME is set to a writable tmp dir for the non-root user.
docker run --rm \
    "${DOCKER_PLATFORM_ARGS[@]}" \
    --user "$(id -u):$(id -g)" \
    -e HOME=/tmp/fdhome \
    -e ANTHROPIC_API_KEY="$API_KEY" \
    -e OPENAI_API_KEY="$API_KEY" \
    -e FD_CLAUDE_MODEL="${FD_CLAUDE_MODEL:-}" \
    -e FD_OPENAI_MODEL="${FD_OPENAI_MODEL:-gpt-5.4}" \
    -e AF_FD_ALLOW_NON_REPRO_TD="${AF_FD_ALLOW_NON_REPRO_TD:-}" \
    -e KEEP_SOURCE="${KEEP_SOURCE:-}" \
    -e KEEP_FLAKY_M2="${KEEP_FLAKY_M2:-}" \
    -v "$FLAKYDOCTOR_DIR":/work \
    -w /work \
    ${EXTRA_RUN_ARGS[@]+"${EXTRA_RUN_ARGS[@]}"} \
    "$IMAGE" \
    bash -lc 'mkdir -p /tmp/fdhome && \
        if [[ -f /usr/lib/x86_64-linux-gnu/libnss_wrapper.so ]]; then \
          printf "fduser:x:%s:%s:FlakyDoctor user:/tmp/fdhome:/bin/bash\n" "$(id -u)" "$(id -g)" > /tmp/fd.passwd; \
          printf "fdgroup:x:%s:\n" "$(id -g)" > /tmp/fd.group; \
          export NSS_WRAPPER_PASSWD=/tmp/fd.passwd NSS_WRAPPER_GROUP=/tmp/fd.group LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libnss_wrapper.so; \
        fi && \
        python3 -u '"$DRIVER"' \
        --test-config "'"$(python3 -c 'import os,sys; print(os.path.relpath(sys.argv[1], sys.argv[2]))' "$TEST_CONFIG" "$FLAKYDOCTOR_DIR")"'" \
        --container "'"$CONTAINER"'" \
        '"$MODE_ARGS"' \
        --model "'"$RUN_MODEL"'" \
        --api-key "$'"$API_KEY_ENV_NAME"'" '"$*"
