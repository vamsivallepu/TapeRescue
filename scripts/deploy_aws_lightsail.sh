#!/usr/bin/env bash
# Deploy the TapeRescue container image to an Amazon Lightsail container service.
#
# Usage: scripts/deploy_aws_lightsail.sh [--build]
#
#   --build   rebuild the local image (docker build -t taperescue:local .) first
#
# Every AWS call uses the `personal` profile only. Override the region or names
# with the environment variables below; the profile is fixed on purpose.
#
#   TAPERESCUE_AWS_REGION   default us-east-2
#   TAPERESCUE_LS_SERVICE   default taperescue
#   TAPERESCUE_LS_POWER     default micro (0.25 vCPU, 1 GB RAM)
#   TAPERESCUE_IMAGE        default taperescue:local
#
# Requires: aws CLI v2, docker, and the lightsailctl plugin on PATH.
# The service runs on one node on purpose: sessions and their temporary files
# live inside the container, so a second node would not find them.
#
# Teardown (stops all charges for this service):
#   aws lightsail delete-container-service --service-name taperescue \
#       --profile personal --region us-east-2
set -euo pipefail

PROFILE=personal
REGION="${TAPERESCUE_AWS_REGION:-us-east-2}"
SERVICE="${TAPERESCUE_LS_SERVICE:-taperescue}"
POWER="${TAPERESCUE_LS_POWER:-micro}"
IMAGE="${TAPERESCUE_IMAGE:-taperescue:local}"
LABEL=app
CONTAINER=app
PORT=8000

aws_ls() { aws lightsail "$@" --profile "$PROFILE" --region "$REGION"; }
die() { echo "error: $*" >&2; exit 1; }
log() { echo "== $*"; }

cd "$(dirname "$0")/.."

command -v aws >/dev/null || die "aws CLI not found"
command -v docker >/dev/null || die "docker not found"
command -v lightsailctl >/dev/null || die "lightsailctl not found; see https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-install-software.html"

if [[ "${1:-}" == "--build" ]]; then
    log "building $IMAGE from $(git rev-parse --short HEAD)"
    docker build -t "$IMAGE" .
fi
docker image inspect "$IMAGE" >/dev/null 2>&1 || die "image $IMAGE not found; run with --build"

log "account $(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text), region $REGION"

state() { aws_ls get-container-services --service-name "$SERVICE" --query 'containerServices[0].state' --output text 2>/dev/null || true; }

if [[ -z "$(state)" ]]; then
    log "creating container service $SERVICE (power $POWER, scale 1)"
    aws_ls create-container-service --service-name "$SERVICE" --power "$POWER" --scale 1 \
        --tags key=project,value=taperescue >/dev/null
fi

wait_for() {
    local want="$1" limit="$2" s i
    for ((i = 0; i < limit; i += 10)); do
        s="$(state)"
        [[ " $want " == *" $s "* ]] && { echo "$s"; return 0; }
        echo "   state: $s"
        sleep 10
    done
    die "service $SERVICE did not reach [$want] within ${limit}s (last: $s)"
}

log "waiting for the service to be ready"
wait_for "READY RUNNING" 900 >/dev/null

log "pushing $IMAGE"
push_out="$(aws_ls push-container-image --service-name "$SERVICE" --label "$LABEL" --image "$IMAGE")"
echo "$push_out"
ref="$(grep -oE ":$SERVICE\.$LABEL\.[0-9]+" <<<"$push_out" | tail -1)"
[[ -n "$ref" ]] || die "could not read the pushed image reference"

containers="$(printf '{"%s":{"image":"%s","ports":{"%s":"HTTP"}}}' "$CONTAINER" "$ref" "$PORT")"
endpoint="$(printf '{"containerName":"%s","containerPort":%s,"healthCheck":{"path":"/","successCodes":"200","intervalSeconds":10,"timeoutSeconds":5,"healthyThreshold":2,"unhealthyThreshold":3}}' "$CONTAINER" "$PORT")"

log "deploying $ref"
aws_ls create-container-service-deployment --service-name "$SERVICE" \
    --containers "$containers" --public-endpoint "$endpoint" >/dev/null

log "waiting for the deployment to go live"
sleep 20
wait_for "RUNNING" 1200 >/dev/null
for ((i = 0; i < 1200; i += 10)); do
    d="$(aws_ls get-container-services --service-name "$SERVICE" --query 'containerServices[0].currentDeployment.[state,containers.app.image]' --output text)"
    [[ "$d" == "ACTIVE"*"$ref" ]] && break
    echo "   deployment: $d"
    sleep 10
done
[[ "$d" == "ACTIVE"*"$ref" ]] || die "deployment of $ref is not active (last: $d)"

url="$(aws_ls get-container-services --service-name "$SERVICE" --query 'containerServices[0].url' --output text)"
log "live at $url"
